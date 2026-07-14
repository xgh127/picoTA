"""Feishu custom bot webhook client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class FeishuWebhookError(RuntimeError):
    """Raised when a Feishu webhook request fails."""


@dataclass(frozen=True)
class FeishuWebhookResponse:
    http_status: int
    body: str
    data: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        if not 200 <= self.http_status < 300:
            return False
        if not self.data:
            return True
        if "code" in self.data:
            return self.data.get("code") == 0
        if "StatusCode" in self.data:
            return self.data.get("StatusCode") == 0
        return True


def send_feishu_webhook(
    webhook_url: str,
    payload: dict,
    timeout: float = 10.0,
) -> FeishuWebhookResponse:
    """Send a Feishu interactive card payload to a custom bot webhook."""
    if not webhook_url:
        raise ValueError("webhook_url is required")

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed = _parse_json(body)
            result = FeishuWebhookResponse(response.status, body, parsed)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        parsed = _parse_json(body)
        raise FeishuWebhookError(f"Feishu webhook HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise FeishuWebhookError(f"Feishu webhook request failed: {exc.reason}") from exc

    if not result.ok:
        raise FeishuWebhookError(f"Feishu webhook returned failure: {result.body}")
    return result


def _parse_json(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
