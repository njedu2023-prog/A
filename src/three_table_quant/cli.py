from __future__ import annotations

import argparse
import json

from .pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the strict three-table shadow pipeline")
    parser.add_argument("--config", default="config/system.json")
    args = parser.parse_args()
    dashboard = run_pipeline(args.config)
    print(
        json.dumps(
            {
                "status": dashboard["current_run"]["status"],
                "intersection_count": dashboard["current_run"].get("intersection_count"),
                "generated_at": dashboard["generated_at"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

