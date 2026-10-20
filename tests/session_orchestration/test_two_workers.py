import asyncio
import json
import pytest
from artemis.runtime.session_orchestration.main_session import MainSession
from artemis.runtime.session_orchestration.models import (
    TaskSpec,
    Completion,
    WorkflowTask,
    WorkflowSpec,
    RoleSpec,
    Dependency,
)


class NoReplan:
    async def propose(self, *args, **kwargs):
        raise AssertionError("Static successful workflow must not ask a model")


def make_spec(goal, outputs=None, schema=None, delay=0.02):
    return TaskSpec(
        goal=goal,
        completion=[Completion(id="done", description=goal)],
        inputs={"_test_delay": delay, "_test_outputs": outputs or {}},
        outputs_schema=schema
        or {"type": "object", "properties": {}, "additionalProperties": False},
        execution_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_two_real_processes_keep_role_context_and_transfer_result(tmp_path):
    a1 = WorkflowTask(role_id="host", spec=make_spec("login"))
    schema = {
        "type": "object",
        "properties": {"meeting_id": {"type": "string"}},
        "required": ["meeting_id"],
        "additionalProperties": False,
    }
    a2 = WorkflowTask(
        role_id="host",
        spec=make_spec("create", {"meeting_id": "001234"}, schema),
        depends_on=[Dependency(task_id=a1.task_id)],
    )
    b1spec = make_spec("apply")
    b1spec.inputs["meeting_id"] = {"from_task": a2.task_id, "key": "meeting_id"}
    b1 = WorkflowTask(role_id="guest", spec=b1spec, depends_on=[Dependency(task_id=a2.task_id)])
    a3 = WorkflowTask(
        role_id="host", spec=make_spec("approve"), depends_on=[Dependency(task_id=a2.task_id)]
    )
    workflow = WorkflowSpec(
        goal="meeting",
        roles=[
            RoleSpec(role_id="host", device_id="test-device-a"),
            RoleSpec(role_id="guest", device_id="test-device-b"),
        ],
        tasks=[a1, a2, b1, a3],
        root=tmp_path,
        max_workflow_seconds=10,
        max_artifact_bytes=10000000,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=5,
        close_timeout_seconds=5,
        fake_worker=True,
    )
    main = MainSession(planner=NoReplan())
    result = await asyncio.wait_for(main.run(workflow), 45)
    assert result["business_status"] == "completed", result
    assert main.workers["host"].process.pid != main.workers["guest"].process.pid
    assert all(w.process.returncode == 0 for w in main.workers.values())
    evidence = list(
        (workflow.root / workflow.workflow_id / "traces" / main.workers["host"].session_id).glob(
            "tasks/*/*/test_evidence.json"
        )
    )
    histories = [json.loads(p.read_text())["history"] for p in evidence]
    assert ["login", "create", "approve"] in histories
    received = list(
        (workflow.root / workflow.workflow_id / "tasks" / b1.task_id).glob("*/request.json")
    )
    assert json.loads(received[0].read_text())["payload"]["inputs"]["meeting_id"] == "001234"
