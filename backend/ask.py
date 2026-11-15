"""Ask the council from the command line (no web UI).

    uv run python -m backend.ask "Your question"
    uv run python -m backend.ask --json "Your question"

This is also the integration seam for other front ends (e.g. a Slack bot):
call `backend.council.run_full_council(question)` and post the stages.
"""

import argparse
import asyncio
import json

from .council import run_full_council


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the LLM Council a question.")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true", help="print all stages as JSON")
    args = parser.parse_args()

    stage1, stage2, stage3, metadata = asyncio.run(run_full_council(args.question))

    if args.json:
        print(json.dumps(
            {"stage1": stage1, "stage2": stage2, "stage3": stage3, "metadata": metadata},
            indent=2,
        ))
        return

    for r in stage1:
        print(f"## {r['model']}\n\n{r['response']}\n")
    if metadata.get("aggregate_rankings"):
        print("## Peer ranking (average position, lower is better)\n")
        for a in metadata["aggregate_rankings"]:
            print(f"- {a['model']}: {a['average_rank']}")
        print()
    print(f"## Chairman ({stage3['model']})\n\n{stage3['response']}")


if __name__ == "__main__":
    main()
