from dataclasses import dataclass

#声明工具风险，risky=True 不是装饰信息。它会进入执行器，决定这个工具是否需要审批。
@dataclass(frozen=True)
class ToolSpec:
    risky: bool


TOOL_SPECS = {
    "read_file": ToolSpec(risky=False),
    "write_file": ToolSpec(risky=True),
}
# 定义结构化结果，这些信息不是给模型看的漂亮日志，而是给运行时、测试和复盘用的证据
@dataclass
class ToolExecutionResult:
    content: str
    metadata: dict

# 写路径边界，这段代码应该在工具执行前运行。不要把路径边界散落到每个工具里，否则后面加 patch_file、run_shell 时很容易漏
from pathlib import Path


def resolve_in_workspace(root: Path, value: str) -> Path:
    root = root.resolve()
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes workspace")
    return candidate
class ToolExecutor:
    def __init__(self, root: Path, approval_policy: str = "auto"):
        self.root = root
        self.approval_policy = approval_policy

    def execute(self, name: str, args: dict) -> ToolExecutionResult:
        spec = TOOL_SPECS.get(name)
        if spec is None:
            return ToolExecutionResult(
                content=f"error: unknown tool {name}",
                metadata={"tool_status": "rejected", "tool_name": name},
            )

        if spec.risky and self.approval_policy == "never":
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata={
                    "tool_status": "rejected",
                    "tool_name": name,
                    "read_only": False,
                    "workspace_changed": False,
                    "affected_paths": [],
                },
            )

        try:
            if name == "write_file":
                target = resolve_in_workspace(self.root, args["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args["content"], encoding="utf-8")
                return ToolExecutionResult(
                    content=f"wrote {args['path']}",
                    metadata={
                        "tool_status": "ok",
                        "tool_name": name,
                        "read_only": False,
                        "workspace_changed": True,
                        "affected_paths": [args["path"]],
                    },
                )

            if name == "read_file":
                target = resolve_in_workspace(self.root, args["path"])
                return ToolExecutionResult(
                    content=target.read_text(encoding="utf-8"),
                    metadata={
                        "tool_status": "ok",
                        "tool_name": name,
                        "read_only": True,
                        "workspace_changed": False,
                        "affected_paths": [],
                    },
                )

        except Exception as exc:
            return ToolExecutionResult(
                content=f"error: tool {name} failed: {exc}",
                metadata={"tool_status": "error", "tool_name": name},
            )

        return ToolExecutionResult(
            content=f"error: unknown tool {name}",
            metadata={"tool_status": "rejected", "tool_name": name},
        )
if __name__ == "__main__":
    root = Path(".")

    allowed = ToolExecutor(root, approval_policy="auto")
    result = allowed.execute("write_file", {"path": "scratch/out.txt", "content": "hello"})
    print(result.content)
    print(result.metadata)

    denied = ToolExecutor(root, approval_policy="never")
    result = denied.execute("write_file", {"path": "scratch/blocked.txt", "content": "no"})
    print(result.content)
    print(result.metadata)