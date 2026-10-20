"""Model adapter for Main; it never creates a native device Context."""

import json
from types import SimpleNamespace
from artemis.runtime.session_orchestration.models import PlanProposal


class MainPlanner:
    def __init__(self):
        self.history = []

    async def propose(self, workflow, scheduler, facts, diagnostics=None):
        from langchain_core.messages import HumanMessage, SystemMessage
        from artemis.config import get_default_llm_config
        from artemis.llm.router import ModelFactory
        from artemis.services.llm import _resolve_endpoint, acomplete_structured

        ctx = SimpleNamespace(llm_config=get_default_llm_config())
        llm = ModelFactory.get_model(_resolve_endpoint(ctx, "planner"))
        context = {
            "goal": workflow.goal,
            "constraints": workflow.constraints,
            "expected_cancellations": [item.model_dump() for item in workflow.expected_cancellations],
            "roles": [r.model_dump() for r in workflow.roles],
            "revision": scheduler.revision,
            "tasks": [t.model_dump(mode="json") for t in scheduler.tasks.values()],
            "results": {k: v.model_dump(mode="json") for k, v in scheduler.results.items()},
            "facts": [
                f
                for f in facts.versions
                if facts.sources.get(f["task_id"]) == (f["source_role"], f["child_session_id"])
            ][-100:],
            "invalidated_tasks": sorted(scheduler.invalidated),
            "diagnostics": diagnostics,
            "recoveries": scheduler.recoveries,
            "recent_proposals": self.history[-10:],
        }
        messages = [
            SystemMessage(
                content=(
                    "You are Main, coordinating persistent Artemis roles. Propose a valid acyclic "
                    "task graph. Workers only execute assigned tasks. Every task needs explicit "
                    "completion criteria and typed outputs. Split submission from approval to avoid "
                    "deadlocks. Preserve executed tasks. Use UUID task IDs and input bindings "
                    "{from_task: UUID, key: output_key}. Do not retry uncertain side effects. "
                    "Request inspect/observe diagnostics when evidence is insufficient; you will get "
                    "their results on the next call. On failure, add explicit reconciliation or recovery "
                    "tasks and a recoveries mapping. Uncertain side effects can only be replaced by "
                    "read_only tasks verifying whether the original intended state was reached. "
                    "Never claim completion from an observation unrelated to the original contract. "
                    "For FAULTED/EXITED roles use restart_roles before scheduling recovery. A restart "
                    "loses in-memory context and invalidates that role's old results, including tasks "
                    "without output fields. Invalidated results are history only; add read-only "
                    "reconciliation with the original contract and rebind unexecuted dependents "
                    "to the new tasks before proceeding. "
                    "Recovery replacements must keep the original role_id, every required completion "
                    "id and description verbatim, and the identical outputs_schema. Map each original "
                    "failed_task_id to replacement_task_ids using recoveries. Scheduler validation errors "
                    "appear as plan_rejected diagnostics: correct those errors in your next proposal; "
                    "a rejected proposal made no changes to the current plan. "
                    "Only tasks explicitly listed by role_id and display_name in expected_cancellations "
                    "may declare expected_terminal_statuses=['succeeded','cancelled']; declare that on "
                    "those tasks before execution. All other tasks require succeeded. A cancellation "
                    "only satisfies workflow completion after Main's exact-attempt cancellation was accepted; "
                    "it does not prove the task's business completion checks passed. "
                    "Set missing_input when no safe progress is possible. Return PlanProposal JSON.\n"
                    "Diagnostic RPC query contracts: observe accepts only mode ('cached' or 'fresh') "
                    "and timeout_seconds (>0, <=30); inspect accepts only level, offset (>=0), "
                    "limit (1..100), snapshot_id. Do not put purpose or other prose keys in query.\n"
                    "The exact required JSON Schema follows; use its field names and nesting:\n"
                    + json.dumps(PlanProposal.model_json_schema(), ensure_ascii=False)
                )
            ),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
        ]
        proposal = await acomplete_structured(llm, messages, schema=PlanProposal)
        self.history.append(proposal.model_dump(mode="json"))
        self.history[:] = self.history[-10:]
        return proposal
