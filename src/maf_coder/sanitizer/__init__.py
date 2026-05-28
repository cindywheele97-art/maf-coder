"""External content sanitizer (soul.md §7.2).

Every byte that crosses from the open internet into agent context must
pass through `sanitize()`. The sanitizer:

1. Normalizes format (HTML → Markdown-ish plain text; strips
   `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<form>`,
   hidden elements, and zero-width / RLO control characters).
2. Scans for prompt-injection markers and records what it found.
3. Wraps the result with `<external source="..." retrieved="...">…</external>`
   tags plus a downstream-warning preamble.

The output is a `SanitizedContent` model with the wrapped content and
the full list of actions taken, so downstream consumers can audit.

This module has no I/O — fetching is the caller's responsibility. The
sanitizer is a pure transform: `(raw_bytes_or_str, content_type, url) ->
SanitizedContent`.
"""

from __future__ import annotations

from .core import sanitize

__all__ = ["sanitize"]
