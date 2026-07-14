"""Local Feishu event callback server.

This server receives Feishu event callbacks, routes message text through the TA
agent, and sends the generated card back through a configured webhook.

For the minimum demo, keep Feishu event encryption disabled in the developer
console.  URL verification and plaintext message events are supported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .feishu_router import _case_board, handle_feishu_text
from .feishu_webhook import send_feishu_webhook

SEEN_EVENT_IDS: set[str] = set()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, status: int, text: str) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def verify_token(payload: dict, expected_token: str | None) -> bool:
    if not expected_token:
        return True
    token = payload.get("token") or payload.get("header", {}).get("token")
    return token == expected_token


def get_event_id(payload: dict) -> str | None:
    return (
        payload.get("uuid")
        or payload.get("event_id")
        or payload.get("header", {}).get("event_id")
        or payload.get("event", {}).get("message", {}).get("message_id")
    )


def is_url_verification(payload: dict) -> bool:
    return bool(payload.get("challenge")) and payload.get("type") == "url_verification"


def parse_message_content(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text", ""))
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        return str(parsed.get("text", content))
    return content


def clean_mention_text(text: str) -> str:
    text = re.sub(r"@_[A-Za-z0-9_-]+", "", text)
    text = re.sub(r"@\S+", "", text)
    return text.strip()


def extract_message_text(payload: dict) -> str:
    event = payload.get("event", {})
    message = event.get("message", {})
    if message:
        return clean_mention_text(parse_message_content(message.get("content", "")))
    if "text" in event:
        return clean_mention_text(str(event.get("text", "")))
    if "message" in event and isinstance(event["message"], str):
        return clean_mention_text(event["message"])
    return ""


def is_message_event(payload: dict) -> bool:
    event_type = payload.get("header", {}).get("event_type") or payload.get("event", {}).get("type", "")
    return "message" in str(event_type)


def get_sender_type(payload: dict) -> str:
    sender = payload.get("event", {}).get("sender", {})
    return str(sender.get("sender_type") or sender.get("type") or "")


def is_from_human_user(payload: dict) -> bool:
    sender_type = get_sender_type(payload)
    if not sender_type:
        return True
    return sender_type == "user"


def is_agent_generated_text(text: str) -> bool:
    prefixes = (
        "## 已帮你整理成日报",
        "## 已帮你整理成周报",
        "## 当前任务完成度",
        "## 导师同步建议",
        "## 我还没理解你的需求",
        "日报草稿",
        "周报草稿",
    )
    return any(text.strip().startswith(prefix) for prefix in prefixes)


def build_event_result(
    payload: dict,
    webhook_url: str | None,
    board: dict | None,
    case_dir: str | None,
    day_index: int,
    send_reply: bool = True,
) -> dict:
    if not is_message_event(payload):
        return {"handled": False, "reason": "ignored_non_message_event"}

    if not is_from_human_user(payload):
        return {
            "handled": False,
            "reason": "ignored_non_user_sender",
            "sender_type": get_sender_type(payload),
        }

    text = extract_message_text(payload)
    if not text:
        return {"handled": False, "reason": "empty_message_text"}

    if is_agent_generated_text(text):
        return {"handled": False, "reason": "ignored_agent_generated_text"}

    routed = handle_feishu_text(text, board=board, case_dir=case_dir, day_index=day_index)
    response: dict[str, Any] | None = None
    send_error: str | None = None
    if send_reply:
        if not webhook_url:
            raise ValueError("FEISHU_WEBHOOK_URL is required to send replies")
        try:
            webhook_response = send_feishu_webhook(webhook_url, routed["card"])
            response = {
                "http_status": webhook_response.http_status,
                "body": webhook_response.data or webhook_response.body,
            }
        except Exception as exc:  # noqa: BLE001 - acknowledge event to avoid Feishu retries
            send_error = str(exc)

    return {
        "handled": True,
        "text": text,
        "intent": routed["intent"],
        "confidence": routed["confidence"],
        "sent": bool(send_reply and not send_error),
        "send_error": send_error,
        "response": response,
    }


class FeishuEventHandler(BaseHTTPRequestHandler):
    server_version = "PicoTAFeishuServer/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            _json_response(self, 200, {"ok": True})
        else:
            _text_response(self, 404, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/feishu/events":
            _text_response(self, 404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            _json_response(self, 400, {"ok": False, "error": "invalid_json"})
            return

        if "encrypt" in payload:
            _json_response(self, 400, {"ok": False, "error": "encrypted_event_not_supported"})
            return

        expected_token = getattr(self.server, "verification_token", None)
        if not verify_token(payload, expected_token):
            _json_response(self, 403, {"ok": False, "error": "invalid_verification_token"})
            return

        if is_url_verification(payload):
            _json_response(self, 200, {"challenge": payload["challenge"]})
            return

        event_id = get_event_id(payload)
        if event_id and event_id in SEEN_EVENT_IDS:
            _json_response(self, 200, {"ok": True, "deduplicated": True})
            return
        if event_id:
            SEEN_EVENT_IDS.add(event_id)

        try:
            result = build_event_result(
                payload,
                webhook_url=getattr(self.server, "webhook_url", None),
                board=getattr(self.server, "board", None),
                case_dir=getattr(self.server, "case_dir", None),
                day_index=getattr(self.server, "day_index", 1),
                send_reply=getattr(self.server, "send_reply", True),
            )
        except Exception as exc:  # noqa: BLE001 - return callback error safely
            _json_response(self, 500, {"ok": False, "error": str(exc)})
            return

        _json_response(self, 200, {"ok": True, **result})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[feishu_server] {self.address_string()} - {format % args}")


def run_server(
    host: str,
    port: int,
    webhook_url: str | None,
    project_state: str | None,
    case_dir: str | None,
    verification_token: str | None,
    day_index: int,
    send_reply: bool,
) -> None:
    board = _case_board(case_dir, project_state)
    server = ThreadingHTTPServer((host, port), FeishuEventHandler)
    server.webhook_url = webhook_url
    server.board = board
    server.case_dir = case_dir
    server.verification_token = verification_token
    server.day_index = day_index
    server.send_reply = send_reply
    print(f"Feishu event server listening on http://{host}:{port}/feishu/events")
    print("Health check: http://{host}:{port}/health".format(host=host, port=port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Feishu event server...")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local Feishu event callback server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--webhook-url", default=os.getenv("FEISHU_WEBHOOK_URL"))
    parser.add_argument("--project-state", default=os.getenv("TA_PROJECT_STATE_PATH"))
    parser.add_argument("--case-dir", default=os.getenv("TA_CASE_DIR"))
    parser.add_argument("--verification-token", default=os.getenv("FEISHU_VERIFICATION_TOKEN"))
    parser.add_argument("--day-index", type=int, default=int(os.getenv("TA_DAY_INDEX", "1")))
    parser.add_argument("--no-send", action="store_true", help="Process event but do not send Feishu reply.")
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        webhook_url=args.webhook_url,
        project_state=args.project_state,
        case_dir=args.case_dir,
        verification_token=args.verification_token,
        day_index=args.day_index,
        send_reply=not args.no_send,
    )


if __name__ == "__main__":
    main()
