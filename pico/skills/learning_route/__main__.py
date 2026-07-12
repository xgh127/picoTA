"""CLI 入口 — python -m pico.skills.learning_route

TODO[B]: 徐国洪 — 实现完整的 CLI 入口。
  运行方式:
    python -m pico.skills.learning_route example_data.json
    python -m pico.skills.learning_route example_data.json --format mermaid
"""

import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m pico.skills.learning_route <data.json> [--format text|mermaid]")
        sys.exit(1)

    data_path = Path(sys.argv[1])
    data = json.loads(data_path.read_text(encoding="utf-8"))
    fmt = "text"
    if "--format" in sys.argv:
        idx = sys.argv.index("--format")
        if idx + 1 < len(sys.argv):
            fmt = sys.argv[idx + 1]

    topics = data["topics"]
    estimated_hours = data.get("estimated_hours", {})

    # TODO[B]: 串联完整流程
    # 1. topological_sort(topics) → sorted_ids, layers, has_cycle
    # 2. if has_cycle: print cycle info, exit
    # 3. build_schedule(topics, sorted_ids, estimated_hours) → schedule
    # 4. if fmt == "mermaid": render_mermaid(topics, sorted_ids)
    #    else: render_schedule_text(schedule)
    print(f"Loaded {len(topics)} topics from {data_path}")
    print("TODO[B]: implement full pipeline")


if __name__ == "__main__":
    main()
