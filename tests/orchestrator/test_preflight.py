"""Preflight readiness-gate tests.

`run_preflight` answers "can this mission go straight to a real run?" in one
pass: router config valid, provider API keys present, target repo profilable,
Docker + sandbox image ready. All external probes (env, docker, image) are
injectable so these stay hermetic.
"""

from __future__ import annotations

from pathlib import Path

from maf_coder.orchestrator.preflight import PreflightReport, run_preflight

_CONFIG = (
    "version: 1\n"
    "roles:\n"
    "  coder_worker:\n"
    "    primary: {model: anthropic/claude-sonnet, temperature: 0.1, max_tokens: 100}\n"
    "    fallback: []\n"
    "  review_validator:\n"
    "    primary: {model: openai/gpt-5, temperature: 0.0, max_tokens: 100}\n"
    "    fallback: [{model: google/gemini-2.5-pro, temperature: 0.0, max_tokens: 100}]\n"
)

_ALL_KEYS = {
    "ANTHROPIC_API_KEY": "x",
    "OPENAI_API_KEY": "x",
    "GEMINI_API_KEY": "x",
}


def _cfg(tmp_path: Path, body: str = _CONFIG) -> Path:
    p = tmp_path / "droid.yaml"
    p.write_text(body)
    return p


def _valid_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text('[package]\nname = "demo"\n\n[lib]\n')
    return repo


def _status(report: PreflightReport, name_contains: str) -> str:
    for c in report.checks:
        if name_contains in c.name:
            return c.status
    raise AssertionError(f"no check matching {name_contains!r} in {[c.name for c in report.checks]}")


class TestRunPreflight:
    def test_all_green_is_ok(self, tmp_path: Path) -> None:
        report = run_preflight(
            _cfg(tmp_path),
            repo_path=_valid_repo(tmp_path),
            sandbox="docker",
            env=_ALL_KEYS,
            docker_available=lambda: True,
            image_present=lambda _img: True,
        )
        assert report.ok is True
        assert all(c.status != "fail" for c in report.checks)

    def test_missing_key_fails(self, tmp_path: Path) -> None:
        env = dict(_ALL_KEYS)
        del env["OPENAI_API_KEY"]
        report = run_preflight(
            _cfg(tmp_path),
            sandbox="local",
            env=env,
            docker_available=lambda: True,
            image_present=lambda _img: True,
        )
        assert report.ok is False
        assert _status(report, "OPENAI_API_KEY") == "fail"

    def test_google_accepts_either_env_var(self, tmp_path: Path) -> None:
        env = {"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x", "GOOGLE_API_KEY": "x"}
        report = run_preflight(
            _cfg(tmp_path), sandbox="local", env=env
        )
        # google requirement satisfied by GOOGLE_API_KEY (not just GEMINI_API_KEY)
        assert _status(report, "GEMINI_API_KEY") == "pass"

    def test_custom_api_key_env_is_required(self, tmp_path: Path) -> None:
        body = (
            "version: 1\n"
            "roles:\n"
            "  coder_worker:\n"
            "    primary: {model: openai/mimo, api_key_env: MIMO_API_KEY, "
            "temperature: 0.1, max_tokens: 100}\n"
            "    fallback: []\n"
        )
        report = run_preflight(_cfg(tmp_path, body), sandbox="local", env={})
        assert _status(report, "MIMO_API_KEY") == "fail"

    def test_bad_router_config_fails_loud(self, tmp_path: Path) -> None:
        report = run_preflight(
            _cfg(tmp_path, "this: is: not: valid: router: config\n"),
            sandbox="local",
            env=_ALL_KEYS,
        )
        assert report.ok is False
        assert _status(report, "router config") == "fail"

    def test_docker_unavailable_fails_image_warns(self, tmp_path: Path) -> None:
        report = run_preflight(
            _cfg(tmp_path),
            sandbox="docker",
            env=_ALL_KEYS,
            docker_available=lambda: False,
            image_present=lambda _img: True,
        )
        assert report.ok is False
        assert _status(report, "docker daemon") == "fail"
        assert _status(report, "sandbox image") == "warn"

    def test_missing_image_fails(self, tmp_path: Path) -> None:
        report = run_preflight(
            _cfg(tmp_path),
            sandbox="docker",
            env=_ALL_KEYS,
            docker_available=lambda: True,
            image_present=lambda _img: False,
        )
        assert report.ok is False
        assert _status(report, "sandbox image") == "fail"

    def test_local_sandbox_skips_docker_checks(self, tmp_path: Path) -> None:
        report = run_preflight(_cfg(tmp_path), sandbox="local", env=_ALL_KEYS)
        names = " ".join(c.name for c in report.checks)
        assert "docker" not in names.lower()

    def test_unprofilable_repo_fails(self, tmp_path: Path) -> None:
        empty = tmp_path / "not-cargo"
        empty.mkdir()
        report = run_preflight(
            _cfg(tmp_path), repo_path=empty, sandbox="local", env=_ALL_KEYS
        )
        assert report.ok is False
        assert _status(report, "target repo") == "fail"
