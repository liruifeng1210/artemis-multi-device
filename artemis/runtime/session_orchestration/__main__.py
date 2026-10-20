"""Run a workflow from a JSON file."""

import argparse
import asyncio
import json
from pathlib import Path
from artemis.runtime.session_orchestration.main_session import MainSession
from artemis.runtime.session_orchestration.models import WorkflowSpec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate contracts and dependencies without starting workers",
    )
    options = parser.parse_args()
    workflow = WorkflowSpec.model_validate_json(options.workflow.read_text(encoding="utf-8"))
    if options.validate_only:
        from artemis.runtime.session_orchestration.scheduler import Scheduler

        Scheduler(workflow)
        print(json.dumps({"valid": True, "workflow_id": workflow.workflow_id}))
        return
    result = asyncio.run(MainSession().run(workflow))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["business_status"] != "completed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
