"""Push adapters for StatusReport delivery (Phase E E-comms / E2).

Why this exists:
    A StatusReport is rendered to disk (status_<n>.md / .json) by the status
    hook, but the user may also want it *pushed* out of band — to a webhook, a
    chat channel, a CLI tail. The delivery mechanism is deployment-specific and
    must never block or crash the supervision loop, so it is isolated behind a
    tiny adapter interface.

Design:
    - ``PushAdapter`` is the ABC: one async ``send(report)`` method.
    - ``NullPushAdapter`` is the default — does nothing (the rendered files on
      disk are the source of truth; tailing them is the CLI default).
    - ``WebhookPushAdapter`` POSTs the report JSON to a URL. It NEVER makes a
      live network call itself: the HTTP transport is injected as a callable so
      tests can substitute a stub. Production wiring passes a real client.

    Selection is config-driven (see ``MissionConfig.push_adapter``); the default
    is ``NullPushAdapter``. The status hook swallows any adapter error so a
    failed push never changes the mission result.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from ..schemas import StatusReport

logger = logging.getLogger(__name__)


class PushAdapter(ABC):
    """Out-of-band delivery channel for a rendered StatusReport."""

    @abstractmethod
    async def send(self, report: StatusReport) -> None:
        """Deliver ``report`` to the channel. Implementations should be quick.

        The caller (status hook) isolates errors, but implementations should
        still fail safe and avoid raising for routine delivery failures.
        """


class NullPushAdapter(PushAdapter):
    """Default no-op adapter. The on-disk status_<n>.md/.json is the channel.

    A CLI ``tail`` of ``status_reports/`` is the intended default UX; nothing
    needs to be pushed for that to work.
    """

    async def send(self, report: StatusReport) -> None:
        logger.debug(
            "NullPushAdapter: status report #%d ready on disk (no push)",
            report.report_number,
        )


# An injected async HTTP transport: (url, json_payload) -> None.
# Kept as a bare callable so tests pass a stub and production passes a real
# client without this module importing any HTTP library.
HttpPost = Callable[[str, dict[str, Any]], Awaitable[None]]


class WebhookPushAdapter(PushAdapter):
    """POST the report JSON to a webhook URL via an injected async HTTP client.

    The transport is injected (``http_post``) so this class never imports or
    invokes a live network library — tests pass a recording stub. Delivery
    failures are logged and swallowed: a status push is best-effort.
    """

    def __init__(self, url: str, http_post: HttpPost) -> None:
        self.url = url
        self._http_post = http_post

    async def send(self, report: StatusReport) -> None:
        payload = report.model_dump(mode="json")
        try:
            await self._http_post(self.url, payload)
        except Exception as e:
            # Best-effort delivery: a failed webhook must not propagate into the
            # supervision loop.
            logger.warning(
                "WebhookPushAdapter: POST to %s failed for report #%d: %r",
                self.url,
                report.report_number,
                e,
            )


__all__ = ["HttpPost", "NullPushAdapter", "PushAdapter", "WebhookPushAdapter"]
