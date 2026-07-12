from pathlib import Path
import json
import re

# 简易工具调用模型
class ScriptedModel:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str, max_new_tokens: int = 512) -> str:
        self.calls += 1
        if self.calls == 1:
            return '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>'
        return "<final>I read README.md through a tool.</final>"

# 解析tool call
def parse_model_output(raw: str):
    tool_match = re.search(r"<tool>(.*?)</tool>", raw, re.DOTALL)
    if tool_match:
        return "tool", json.loads(tool_match.group(1))

    final_match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
    if final_match:
        return "final", final_match.group(1).strip()

    return "retry", "model returned neither <tool> nor <final>"



def read_file(root: Path, path: str, start: int = 1, end: int = 40) -> str:
    target = (root / path).resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes workspace")

    lines = target.read_text(encoding="utf-8").splitlines()
    selected = lines[start - 1:end]
    return "\n".join(f"{idx}: {line}" for idx, line in enumerate(selected, start=start))


# 能调用工具的runtime，能观察文件系统
class ToolCallingRuntime:
    def __init__(self, model, root: Path):
        self.model = model
        self.root = root
        self.history = []

    def build_prompt(self, user_message: str) -> str:
        history_text = "\n".join(self.history)
        return f"""You are a small coding agent.

Return exactly one of:
<tool>{{"name":"read_file","args":{{"path":"README.md","start":1,"end":20}}}}</tool>
<final>answer</final>

History:
{history_text}

User:
{user_message}
"""

    def ask(self, user_message: str) -> str:
        for _ in range(4):
            prompt = self.build_prompt(user_message)
            raw = self.model.complete(prompt)
            kind, payload = parse_model_output(raw)
            # 如果是tool call，执行工具调用
            if kind == "tool":
                name = payload["name"]
                args = payload.get("args", {})
                if name != "read_file":
                    self.history.append(f"Tool error: unknown tool {name}")
                    continue

                content = read_file(self.root, **args)
                self.history.append(f"Tool result: read_file\n{content}")
                continue

            if kind == "final":
                return payload
            # 否则，记录错误，retry
            self.history.append(f"Parse error: {payload}")

        return "stopped: step limit reached"
if __name__ == "__main__":
    agent = ToolCallingRuntime(ScriptedModel(), Path("."))
    print(agent.ask("read the README"))
    print("--- history ---")
    print("\n".join(agent.history))