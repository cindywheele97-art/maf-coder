# MAF-Coder Rust Sandbox
#
# 通用 Rust 多天任务沙箱。装齐 stable + nightly + 所有常用 cargo 子命令工具链，
# 以及 C/C++ 工具链、protoc、wasm-pack、安全扫描器、git CLI 集成。
#
# 持久化 volume 设计（在 docker run 时 mount）：
#   /cargo-cache         → CARGO_HOME（crates.io 索引 + 已下载源码 + 已构建的 ~/.cargo/bin 工具）
#   /target-cache        → 全局共享 target/（通过 CARGO_TARGET_DIR 指定）
#   /sccache             → sccache 编译缓存（通过 SCCACHE_DIR）
#   /workspace           → mission 工作目录（git worktree 挂这里）
#   /missions            → 工件黑板（mission_id 子目录）
#
# 镜像大小预估：4-5 GB（接受，多天任务镜像不是边缘部署场景）

FROM rust:1.85-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_HOME=/cargo-cache \
    RUSTUP_HOME=/cargo-cache/rustup \
    CARGO_TARGET_DIR=/target-cache \
    SCCACHE_DIR=/sccache \
    RUSTC_WRAPPER=sccache \
    PATH=/cargo-cache/bin:/usr/local/cargo/bin:/root/.cargo/bin:$PATH

# ============================================================================
# 系统工具与 C 工具链
# ============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        openssh-client \
        build-essential \
        gcc \
        g++ \
        clang \
        lld \
        mold \
        cmake \
        make \
        pkg-config \
        libssl-dev \
        libsqlite3-dev \
        zlib1g-dev \
        protobuf-compiler \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        fd-find \
        jq \
        unzip \
        less \
        vim-tiny \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# ============================================================================
# Rust 工具链：stable + nightly + 关键 components
# ============================================================================
RUN rustup toolchain install stable --component clippy rustfmt rust-analyzer rust-src \
    && rustup toolchain install nightly --component clippy rustfmt rust-src miri \
    && rustup target add wasm32-unknown-unknown wasm32-wasi \
    && rustup default stable

# ============================================================================
# Cargo 工具全家桶
# ----------------------------------------------------------------------------
# audit: 依赖漏洞扫描（Security Worker 主力）
# deny: license / 重复依赖 / 来源审查（Security Worker 主力）
# geiger: unsafe 代码扫描（Security Worker 主力）
# expand: 宏展开看实际代码（Research Worker）
# machete: 未使用依赖（Coder/Security 都用）
# outdated: 依赖更新检查（Research/Security）
# nextest: 比 cargo test 快 1.5-3x（Coder + ReviewValidator）
# watch: 增量重跑（开发阶段用）
# udeps: 未使用依赖检测（与 machete 互补）
# bloat: 二进制大小分析（BehaviorValidator wasm 探针用）
# ============================================================================
RUN cargo install sccache --locked && \
    cargo install cargo-audit --locked && \
    cargo install cargo-deny --locked && \
    cargo install cargo-geiger --locked && \
    cargo install cargo-expand --locked && \
    cargo install cargo-machete --locked && \
    cargo install cargo-outdated --locked && \
    cargo install cargo-nextest --locked && \
    cargo install cargo-watch --locked && \
    cargo install cargo-udeps --locked && \
    cargo install cargo-bloat --locked && \
    rm -rf /cargo-cache/registry/cache /cargo-cache/registry/src

# ============================================================================
# WebAssembly 工具链
# ============================================================================
RUN cargo install wasm-pack --locked && \
    cargo install wasm-bindgen-cli --locked && \
    cargo install twiggy --locked && \
    rm -rf /cargo-cache/registry/cache /cargo-cache/registry/src

# ============================================================================
# Node.js（wasm-pack test --node 需要 / 部分 build.rs 需要）
# ============================================================================
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ============================================================================
# Git CLI 工具：gh (GitHub) + glab (GitLab) — PR 创建流程必备
# ============================================================================
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
        dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://gitlab.com/gitlab-org/cli/-/raw/main/scripts/install.sh | bash || true

# ============================================================================
# 安全扫描器（Security Worker 用）
# ----------------------------------------------------------------------------
# gitleaks 的 release asset 名包含版本号，所以先查 latest tag 再构造 URL。
# 锁版本时把 GITLEAKS_VER 改成具体版本（例如 8.21.0）以提高可复现性。
# ============================================================================
RUN GITLEAKS_VER=$(curl -fsSL https://api.github.com/repos/gitleaks/gitleaks/releases/latest \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4 | sed 's/^v//') && \
    echo "Installing gitleaks ${GITLEAKS_VER}" && \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VER}/gitleaks_${GITLEAKS_VER}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks && \
    chmod +x /usr/local/bin/gitleaks

RUN curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \
        | sh -s -- -b /usr/local/bin

# ============================================================================
# Python 工具（Sandbox 内运行 Python 探针 / 辅助脚本时需要）
# ============================================================================
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        httpx \
        pydantic \
        pyyaml \
        rich

# ============================================================================
# Sandbox 用户与权限
# ----------------------------------------------------------------------------
# 不用 root 跑 cargo（防止权限污染 host 挂载的 volume）
# ============================================================================
RUN useradd -m -u 1000 -s /bin/bash maf && \
    mkdir -p /workspace /missions /cargo-cache /target-cache /sccache /user_messages && \
    chown -R maf:maf /workspace /missions /cargo-cache /target-cache /sccache /user_messages

USER maf
WORKDIR /workspace

# ============================================================================
# 健康检查：确保关键工具都装好
# ============================================================================
RUN cargo --version && \
    rustup --version && \
    cargo clippy --version && \
    cargo fmt --version && \
    cargo audit --version && \
    cargo deny --version && \
    cargo nextest --version && \
    sccache --version && \
    git --version && \
    gh --version && \
    gitleaks version && \
    node --version

# ============================================================================
# 默认 entrypoint：sleep，让 host 端 orchestrator 通过 docker exec 注入命令
# ----------------------------------------------------------------------------
# 多天任务下容器是长生命周期的，host 端通过 exec 触发各 Worker 操作
# ============================================================================
CMD ["sleep", "infinity"]


# ============================================================================
# 使用示例（host 端 orchestrator 起容器时）：
# ----------------------------------------------------------------------------
#
# docker volume create maf-cargo-cache
# docker volume create maf-target-cache
# docker volume create maf-sccache
#
# docker run -d \
#   --name maf-sandbox-<mission_id> \
#   -v maf-cargo-cache:/cargo-cache \
#   -v maf-target-cache:/target-cache \
#   -v maf-sccache:/sccache \
#   -v $(pwd)/workspace:/workspace \
#   -v $(pwd)/missions:/missions \
#   -v $(pwd)/user_messages:/user_messages \
#   --memory=16g \
#   --cpus=4 \
#   --network=bridge \
#   maf-coder:rust-sandbox
#
# 然后通过 docker exec 触发 worker 操作：
#
# docker exec -u maf maf-sandbox-<mission_id> \
#   bash -lc "cd /workspace/repo && cargo test --workspace --all-features"
#
# ============================================================================
