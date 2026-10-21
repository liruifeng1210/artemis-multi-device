"""Opt-in real CLI protocol smoke, with synthetic text only and no device actions.

Run from the patched MP checkout after its mock regression has passed. This is
not a real-device acceptance test and must never be counted as one.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from artemis.agents.checker.checker import _declared_report_model, _structured_report
from artemis.context import ArtemisContext, DeviceContext, ExecutionSetup
from artemis.data_engine.context_vars import CURRENT_TRACE_ID
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.llm.codex import CodexChatModel
from artemis.llm.reliability import Failure, FailureCategory, LLMPermanentError
from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.runtime.session_scope import TaskScope
from artemis.services.llm import RobustChatModelWrapper


class BoundedProtocolCodex(CodexChatModel):
    # Shared through adapter model_copy() transformations; no third CLI launch.
    cli_attempts: list[int] = Field(default_factory=list, exclude=True)

    async def _agenerate(self, *args, **kwargs):
        if len(self.cli_attempts) >= 2:
            raise LLMPermanentError(
                "Protocol smoke permits at most two CLI invocations",
                failure=Failure(FailureCategory.BAD_REQUEST, False, False),
            )
        self.cli_attempts.append(len(self.cli_attempts) + 1)
        return await super()._agenerate(*args, **kwargs)


class CallbackCounter(BaseCallbackHandler):
    def __init__(self):
        self.started, self.ended, self.failed = [], [], []

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started.append(str(run_id))

    def on_llm_end(self, response, *, run_id, **kwargs):
        self.ended.append(str(run_id))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.failed.append(str(run_id))


async def main(output_root):
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # No device controller, SDK Session, graph, driver, hierarchy, or screenshot.
    DataEngine._connect_ipc_locked = lambda *args, **kwargs: False
    ctx = ArtemisContext(device=DeviceContext(device_id="SYNTHETIC_PROTOCOL_NO_DEVICE"),
                         execution_setup=ExecutionSetup(traces_path=root, trace_name="checker-codex-protocol"))
    ctx.execution_owner = {"role_id": "protocol-only", "workflow_id": "synthetic-protocol"}
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session(goal="Synthetic text identity protocol; not real-device acceptance")
    scope = TaskScope(str(uuid4()), str(uuid4()), "Synthetic text protocol only")
    engine.begin_task(scope)
    parent = uuid4()
    token = CURRENT_TRACE_ID.set(parent)
    counter = CallbackCounter()
    raw = BoundedProtocolCodex(model_name="gpt-6-luna", reasoning_effort="xhigh", timeout_seconds=300)
    endpoint = ModelEndpoint(provider=ModelProvider.CODEX, model_name="gpt-6-luna", reasoning_effort="xhigh", timeout_seconds=300)
    llm = RobustChatModelWrapper(raw.with_config(callbacks=[DataEngineCallbackHandler(ctx), counter]), ctx, endpoint=endpoint)
    items = [
        SimpleNamespace(kind="verify", when="at_end", text="[contract:synthetic_value] The synthetic record explicitly states value=7."),
        SimpleNamespace(kind="verify", when="at_end", text="[contract:synthetic_label] The synthetic record explicitly states label=blue."),
    ]
    config = {"protocol_only": True, "real_device_acceptance": False, "provider": "codex", "model": "gpt-6-luna",
              "reasoning_effort": "xhigh", "cli_timeout_seconds": 300, "max_cli_invocations": 2,
              "max_identity_corrections": 1, "schema": _declared_report_model(items).model_json_schema()}
    (root / "protocol-config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = dict(config)
    try:
        report = await asyncio.wait_for(_structured_report(llm, [
            SystemMessage(content="This is a synthetic text-only protocol test. Judge the declared strings using only the supplied synthetic record. Do not operate, inspect, or claim a real device. Return exact declared identities and truthful judgments; no status is preferred."),
            HumanMessage(content="Synthetic record: value=7; label=red.\nDeclared items:\n" + "\n".join(item.text for item in items)),
        ], items), 620)
        expected = {(item.kind, item.text) for item in items}
        pairs = [(verdict.kind, verdict.item_text) for verdict in report.verdicts]
        assert len(pairs) == len(expected) and set(pairs) == expected, "Protocol identity/coverage failed"
        assert len(counter.started) in (1, 2) and len(set(counter.started)) == len(counter.started), "Callback count invalid"
        assert set(counter.started) == set(counter.ended) and not counter.failed, "Callback terminal mismatch"
        await engine.flush()
        with engine.storage._get_connection() as connection:
            rows = connection.execute("SELECT * FROM traces WHERE type='llm_call'").fetchall()
        assert len(rows) == len(counter.started), "Missing or duplicated LLM trace"
        for row in rows:
            assert row["status"] == "success" and row["parent_trace_id"] == str(parent)
            execution = json.loads(row["payload"])["execution"]
            assert execution["task_id"] == scope.task_id and execution["attempt_id"] == scope.attempt_id
        summary.update(passed=True, report=report.model_dump(), cli_invocations=len(raw.cli_attempts),
                       callback_starts=len(counter.started), callback_ends=len(counter.ended), trace_count=len(rows),
                       session_id=str(engine.current_session_id), parent_trace_id=str(parent))
    except BaseException as error:
        summary.update(passed=False, error_type=type(error).__name__, cli_invocations=len(raw.cli_attempts),
                       callback_starts=len(counter.started), callback_ends=len(counter.ended), callback_errors=len(counter.failed))
        raise
    finally:
        CURRENT_TRACE_ID.reset(token)
        await engine.shutdown()
        (root / "protocol-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("passed", "cli_invocations", "callback_starts", "trace_count")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True)
    asyncio.run(main(parser.parse_args().output_root))
