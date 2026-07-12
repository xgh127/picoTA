import json
import os
import urllib.error
import urllib.request


class FakeModelClient:
    """Deterministic model for learning and tests.

    Main Pico uses real provider clients behind the same `complete()` shape.
    mini-pico defaults to this fake client so the control loop is visible
    without API keys or network calls.
    """

    supports_prompt_cache = False

    def __init__(self, outputs=None):
        self.outputs = list(outputs) if outputs is not None else None
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens=512, **_kwargs):
        self.prompts.append(prompt)
        self.last_completion_metadata = {"model": "FakeModelClient", "max_new_tokens": max_new_tokens}
        if self.outputs is not None:
            if not self.outputs:
                raise RuntimeError("fake model ran out of outputs")
            return self.outputs.pop(0)
        if "Tool result:" in prompt:
            return "<final>mini-pico read the workspace through a tool and returned a final answer.</final>"
        return '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":40}}</tool>'


class OpenAICompatibleClient:
    """适用于 Kimi / DeepSeek / 任何 OpenAI 兼容 API 的模型客户端。"""

    supports_prompt_cache = False

    def __init__(self, model, base_url, api_key, temperature=0):
        self.model = model
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.temperature = temperature
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens=512, **_kwargs):
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_new_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {self.base_url}: {exc.reason}") from exc

        if data.get("error"):
            raise RuntimeError(f"API error: {data['error']}")

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not text:
            raise RuntimeError("API returned empty response")
        self.last_completion_metadata = {
            "model": self.model,
            "usage": data.get("usage", {}),
            "max_new_tokens": max_new_tokens,
        }
        return text
