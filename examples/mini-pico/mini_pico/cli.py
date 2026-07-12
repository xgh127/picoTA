import argparse
import os
import re
from pathlib import Path

from .providers import FakeModelClient, OpenAICompatibleClient
from .runtime import Pico
from .state import RunStore
from .workspace import Workspace


def _load_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            for line in env_path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                name, value = line.split("=", 1)
                name, value = name.strip(), value.strip().strip("\"'")
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                    os.environ[name] = value
            break


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Teaching-sized Pico agent harness.")
    parser.add_argument("prompt", nargs="*", help="One-shot prompt. If omitted, mini-pico starts a small REPL.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--approval", choices=("auto", "never"), default="auto", help="Whether risky tools are allowed.")
    parser.add_argument("--max-steps", type=int, default=4, help="Maximum tool/model iterations.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--model", default="moonshot-v1-8k", help="Model name (default: moonshot-v1-8k).")
    parser.add_argument("--base-url", default="https://api.moonshot.cn", help="API base URL (default: https://api.moonshot.cn).")
    parser.add_argument("--fake", action="store_true", help="Use FakeModelClient instead of real API.")
    return parser


def build_agent(args):
    workspace = Workspace.build(args.cwd)
    run_store = RunStore(workspace.root / ".mini-pico" / "runs")
    if args.fake:
        model_client = FakeModelClient()
    else:
        model_client = OpenAICompatibleClient(
            model=args.model,
            base_url=args.base_url,
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
    return Pico(
        model_client=model_client,
        workspace=workspace,
        run_store=run_store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
    )


def main(argv=None):
    _load_env(Path.cwd())
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)
    prompt = " ".join(args.prompt).strip()
    if prompt:
        print(agent.ask(prompt))
        return 0

    while True:
        try:
            user_input = input("mini-pico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0
        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        print(agent.ask(user_input))
