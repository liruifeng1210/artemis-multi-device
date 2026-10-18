"""Adapters around the existing Pro nodes; one device context per participant."""

import asyncio
import inspect
import json
from pathlib import Path
import threading
import time
from typing import Annotated
from uuid import uuid4

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import InjectedState
from langchain_core.tools.base import InjectedToolCallId

from artemis.graph.state import State
from artemis.runtime.execution_scope import CURRENT_EXECUTION, ExecutionScope, execution_scope
from artemis.runtime.multi_device.plan_policy import (
    RoleBindingValidator,
    role_instructions,
    role_of,
)
from artemis.utils.notes import get_note_file_path
from artemis.utils.plan_grammar import parse_plan
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class MultiDeviceRuntime:
    def __init__(self, role_plan, contexts, *, max_turns=200):
        if set(contexts) != {r.id for r in role_plan.roles}:
            raise ValueError("Every declared role needs exactly one runtime context")
        if len({ctx.device.device_id for ctx in contexts.values()}) != len(contexts):
            raise ValueError("Roles cannot share a device")
        self.role_plan = role_plan
        self.contexts = contexts
        self.control_ctx = next(iter(contexts.values()))
        run_id = str(getattr(self.control_ctx.data_engine, "current_session_id", ""))
        self.correlation_token = "artemis_" + run_id.replace("-", "")
        self.run_id = run_id
        self.policy = RoleBindingValidator(role_plan)
        self.nodes = {}
        self.initial_observations = {}
        self.saved_states = {}
        self.active_role = None
        self.origin_hash = None
        self.turn_id = None
        self.step_id = None
        self.turns = 0
        self.max_turns = max_turns
        self.plan_revision = 0
        self.plan_lock = threading.RLock()
        self.closed = False
        self.halt_reason = None
        self.final_retry_role = None
        self.final_review_status = None
        for role_id, ctx in contexts.items():
            ctx.role_id = role_id
            ctx.multi_device_runtime = self
            ctx.business_bindings_required = True

    @property
    def engine(self):
        return self.control_ctx.data_engine

    @property
    def plan_path(self):
        return get_note_file_path(self.engine.base_dir, "task_plan")

    def read_plan(self):
        return self.plan_path.read_text(encoding="utf-8") if self.plan_path.exists() else ""

    def planning_instructions(self):
        return role_instructions(self.role_plan) + (
            "\nApp/environment instructions:\n" + getattr(self, "app_instructions", "")
        )

    def operator_instructions(self, role_id):
        snapshot = parse_plan(self.read_plan())
        current = next((item for item in snapshot.top_level if item.key == self.origin_hash), None)
        milestone = current.text if current is not None else "Read the current task plan"
        return self.planning_instructions() + (
            f"\nYou currently control ONLY role {role_id}. Current milestone hash: {self.origin_hash}. "
            f"Current milestone: {milestone}. "
            "All earlier milestones in the written plan must be complete before this one runs. "
            "Perform only this milestone and its children during this turn. Do not start work "
            "from a later milestone, even when it has the same role or the app offers that path. "
            "Do not batch a role's future tasks with the current task. "
            "Other roles' facts are available in shared notes, but their coordinates are not yours. "
            f"Use this case token when correlating messages or invitations across devices: "
            f"{self.correlation_token}. Put shared references and necessary non-sensitive "
            "confirmed facts in notes; keep runtime-bound values in their bindings.\n"
        )

    def commit_plan(self, path: Path, before, after, scope):
        with self.plan_lock:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != before:
                logger.warning(
                    f"[multi-device] plan.conflict run_id={self.run_id} phase={scope.phase} role_id={scope.role_id} revision={self.plan_revision}"
                )
                raise ValueError("The plan changed; read it again before updating")
            actor = scope.role_id if scope.phase == "operator" else None
            try:
                self.policy.validate(after, before, actor)
            except ValueError as exc:
                logger.warning(
                    f"[multi-device] plan.rejected run_id={self.run_id} phase={scope.phase} role_id={actor} error_type={type(exc).__name__}"
                )
                raise
            temporary = path.with_suffix(".pending")
            temporary.write_text(after, encoding="utf-8")
            temporary.replace(path)
            if before != after:
                self.plan_revision += 1
                snapshot = parse_plan(after)
                logger.info(
                    f"[multi-device] plan.committed run_id={self.run_id} phase={scope.phase} role_id={actor} revision={self.plan_revision} milestone_count={len(snapshot.top_level)} check_count={len(snapshot.check_items)}"
                )

    def reopen_milestone(self, subgoal_key: str) -> bool:
        """Reopen a failed check and suspend its successor in one plan commit."""
        from artemis.utils.plan_grammar import CHECKBOX_LINE_RE, STATUS_ACTIVE

        with self.plan_lock:
            before = self.read_plan()
            snapshot = parse_plan(before)
            target = next((i for i in snapshot.top_level if i.key == subgoal_key), None)
            if target is None or not target.is_done:
                return False
            lines = before.splitlines()
            owner = None
            for index, line in enumerate(lines):
                match = CHECKBOX_LINE_RE.match(line)
                if match is None:
                    continue
                if not match.group("indent"):
                    owner = next((i for i in snapshot.top_level if i.line_no == index), None)
                status = match.group("status")
                if owner is target and not match.group("indent"):
                    replacement = STATUS_ACTIVE
                elif owner is not target and status == STATUS_ACTIVE:
                    replacement = " "
                else:
                    continue
                lines[index] = (
                    line[: match.start("status")] + replacement + line[match.end("status") :]
                )
            after = "\n".join(lines) + ("\n" if before.endswith("\n") else "")
            self.commit_plan(self.plan_path, before, after, ExecutionScope(phase="checkpoint"))
            logger.info(
                f"[multi-device] milestone.reopened run_id={self.run_id} milestone_hash={subgoal_key} revision={self.plan_revision}"
            )
            return True

    def handoff_requested(self):
        if not self.active_role or not self.origin_hash:
            return False
        target = self.policy.target(self.read_plan())
        return target is None or target.key != self.origin_hash

    def require_device_scope(self, role_id):
        scope = CURRENT_EXECUTION.get()
        if self.closed:
            logger.warning(
                f"[multi-device] scope.rejected run_id={self.run_id} role_id={role_id} reason=closed"
            )
            raise RuntimeError("The case has ended; device operations are no longer allowed")
        if scope is None or scope.runtime is not self or scope.role_id != role_id:
            logger.warning(
                f"[multi-device] scope.rejected run_id={self.run_id} role_id={role_id} reason=role_mismatch"
            )
            raise RuntimeError("Device operation has no matching role scope")
        if scope.phase in ("planner", "preparation", "checkpoint"):
            return
        if scope.turn_id != self.turn_id or role_id != self.active_role or self.handoff_requested():
            logger.warning(
                f"[multi-device] scope.rejected run_id={self.run_id} role_id={role_id} active_role={self.active_role} turn_id={scope.turn_id} current_turn_id={self.turn_id} reason=stale_turn_or_handoff"
            )
            raise RuntimeError(
                "Milestone changed; defer device operations until a fresh observation"
            )

    def guard_tools(self, tools, role_id):
        from artemis.tools.tool_wrapper import invoke_tool_with_injection

        def guarded(tool):
            async def call(
                tool_call_id: Annotated[str, InjectedToolCallId] = None,
                state: Annotated[State, InjectedState] = None,
                **args,
            ):
                from artemis.runtime.action_bindings import require_shell_allowed

                require_shell_allowed(self.contexts[role_id], tool.name, args)
                # Read-only plan access can finish a handoff; all other calls yield.
                if tool.name not in ("read_note", "list_notes"):
                    self.require_device_scope(role_id)
                return await invoke_tool_with_injection(
                    tool,
                    args,
                    tool_call_id or "",
                    state,
                    record_trace=True,
                )

            return StructuredTool.from_function(
                coroutine=call,
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema,
                metadata={**(tool.metadata or {}), "artemis_passthrough": True},
            )

        return [guarded(tool) for tool in tools]

    async def begin_turn(self, state):
        from artemis.graph.checkpoints import read_ledger, harvest_finished_checkpoints, harvest_run

        # All checkpoints belong to the case control context, including checks from past roles.
        findings = []
        pending_count = len(self.control_ctx.checkpoint_tasks)
        logger.info(
            f"[multi-device] turn.select run_id={self.run_id} previous_role={self.active_role} previous_turn_id={self.turn_id} checkpoint_tasks={pending_count} revision={self.plan_revision}"
        )
        if self.control_ctx.checkpoint_tasks:
            tasks = [entry.task for entry in self.control_ctx.checkpoint_tasks.values()]
            timeout = self.control_ctx.execution_setup.settlement_timeout
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                for key, entry in list(self.control_ctx.checkpoint_tasks.items()):
                    if entry.task in pending:
                        del self.control_ctx.checkpoint_tasks[key]
                        findings.extend(
                            harvest_run(
                                self.control_ctx,
                                state,
                                entry,
                                allow_side_effects=False,
                                cancelled_status="unchecked",
                            )
                        )
            # Normal harvest reopens a failed verify on its original role. Exit settlement
            # is bookkeeping-only and must not be used for this handoff barrier.
            findings.extend(harvest_finished_checkpoints(self.control_ctx, state))
        snapshot = parse_plan(self.read_plan())
        complete = {i.key for i in snapshot.top_level if i.is_done}
        declared_checks = {ci.key for ci in snapshot.check_items}
        latest = {}
        for row in read_ledger(self.engine.base_dir):
            latest[(row.get("checkpoint_id"), row.get("check_key") or row.get("item_text"))] = row
        for row in latest.values():
            if (
                row.get("checkpoint_id") in complete
                and row.get("kind") == "verify"
                and (not row.get("check_key") or row["check_key"] in declared_checks)
                and row.get("status") in ("failed", "inconclusive", "error", "timeout", "unchecked")
            ):
                self.halt_reason = "A completion check could not establish the handoff precondition"
                logger.error(
                    f"[multi-device] turn.blocked run_id={self.run_id} checkpoint_id={row.get('checkpoint_id')} check_key={row.get('check_key')} status={row.get('status')}"
                )
                raise RuntimeError(self.halt_reason)
        if self.control_ctx.assert_halt:
            raise RuntimeError("The test halted after an assertion failure")
        target = self.policy.target(self.read_plan())
        if target is None:
            logger.info(
                f"[multi-device] turn.no_target run_id={self.run_id} findings={len(findings)} revision={self.plan_revision}"
            )
            return {}
        self.turns += 1
        if self.turns > self.max_turns:
            raise RuntimeError(f"Case exceeded its {self.max_turns} turn budget")
        role = role_of(target)
        changed = self.active_role is not None and self.active_role != role
        local_fields = (
            "open_incident",
            "last_closed_incident",
            "last_execution_result",
            "operator_feedback",
        )
        update = {}
        if changed:
            logger.info(
                f"[multi-device] role.handoff run_id={self.run_id} from_role={self.active_role} to_role={role} milestone_hash={target.key}"
            )
            previous_ctx = self.contexts[self.active_role]
            from artemis.tools.command_tool import (
                get_adb_task_registry,
                shutdown_adb_background_tasks,
            )

            await asyncio.wait_for(shutdown_adb_background_tasks(previous_ctx), 15)
            if get_adb_task_registry(previous_ctx).background:
                raise RuntimeError("Previous role still owns background ADB processes")
            ledger = getattr(previous_ctx, "transcript_ledger", None)
            if ledger is not None and getattr(ledger, "has_staged_turn", False):
                ledger.commit_staged(
                    step_key=getattr(state, "current_step_id", None),
                    validator_result=(
                        getattr(state, "last_execution_result", None)
                        if getattr(state, "structured_decisions", None)
                        else None
                    ),
                )
            previous_state = {k: getattr(state, k) for k in local_fields}
            if self.final_retry_role == role:
                # Settlement feedback belongs to the role being reopened.
                previous_state["operator_feedback"] = None
            self.saved_states[self.active_role] = previous_state
            update.update(self.saved_states.get(role, {k: None for k in local_fields}))
            if self.final_retry_role == role and state.operator_feedback:
                update["operator_feedback"] = state.operator_feedback
            update.update(
                latest_screenshot=None,
                latest_ui_hierarchy=None,
                operator_raw_data=None,
                indexed_points=None,
                indexed_elements=None,
                subagent_calls=[],
            )
        if findings:
            update["operator_feedback"] = findings
        self.active_role = role
        self.origin_hash = target.key
        self.turn_id = uuid4().hex
        self.step_id = uuid4()
        self.final_retry_role = None
        if changed:
            update.update(
                structured_decisions=None,
                current_step_id=None,
                operator_raw_thinking=None,
                operator_native_thinking=None,
            )
        self.engine._publish(
            "role_activated",
            {
                "role_id": role,
                "device_id": self.contexts[role].device.device_id,
                "subgoal_hash": target.key,
                "turn_id": self.turn_id,
                "plan_revision": self.plan_revision,
            },
        )
        self.save_progress("running")
        logger.info(
            f"[multi-device] turn.started run_id={self.run_id} turn={self.turns} turn_id={self.turn_id} role_id={role} device_id={self.contexts[role].device.device_id} milestone_hash={target.key} revision={self.plan_revision} findings={len(findings)}"
        )
        return update

    async def boundary(self, state):
        if self.handoff_requested():
            logger.info(
                f"[multi-device] turn.deferred run_id={self.run_id} turn_id={self.turn_id} role_id={self.active_role} milestone_hash={self.origin_hash}"
            )
            return {
                "structured_decisions": None,
                "operator_feedback": [
                    "Role handoff: actions deferred pending a fresh observation."
                ],
            }
        return {}

    def execution_edge(self, state):
        from artemis.graph.graph import execution_check_edge

        if self.handoff_requested():
            return "review_subgoals"
        return execution_check_edge(state)

    def scope(self, *, phase, role_id=None, step_id=None, subgoal_hash=None):
        ctx = self.contexts.get(role_id, self.control_ctx)
        return ExecutionScope(
            engine=self.engine,
            runtime=self,
            role_id=role_id,
            device_id=ctx.device.device_id if role_id else None,
            subgoal_hash=subgoal_hash if subgoal_hash is not None else self.origin_hash,
            turn_id=self.turn_id,
            step_id=step_id,
            phase=phase,
        )

    def wrap_node(self, name, control_node):
        async def invoke(state):
            started = time.monotonic()
            is_case = name in ("planner", "exit_settlement")
            phase = "final" if name == "exit_settlement" else name
            role = None if is_case else self.active_role
            node = control_node
            if name in ("perception", "operator", "validator", "summarizer"):
                node = self.nodes[role][name]
            with execution_scope(self.scope(phase=phase, role_id=role, step_id=self.step_id)):
                logger.debug(
                    f"[multi-device] node.start run_id={self.run_id} node={name} role_id={role} turn_id={self.turn_id} step_id={self.step_id}"
                )
                if name == "validator":
                    self.require_device_scope(role)
                result = node(state)
                if inspect.isawaitable(result):
                    result = await result
                if name == "planner":
                    self.policy.validate(self.read_plan())
                if name == "exit_settlement" and result.get("exit_settlement_route") == "continue":
                    self.prepare_final_retry()
                scope = CURRENT_EXECUTION.get()
                if name == "perception":
                    self.step_id = scope.step_id
                elif name == "execution_check" and result.get("current_step_id"):
                    from uuid import UUID

                    self.step_id = UUID(result["current_step_id"])
                logger.debug(
                    f"[multi-device] node.complete run_id={self.run_id} node={name} role_id={role} turn_id={self.turn_id} elapsed_ms={int((time.monotonic() - started) * 1000)}"
                )
                return result

        return invoke

    def prepare_final_retry(self):
        """Map failed final criteria back to their owning milestones before routing."""
        from artemis.graph.checkpoints import read_ledger
        from artemis.utils.plan_grammar import CHECKBOX_LINE_RE, subgoal_hash

        text = self.read_plan()
        snapshot = parse_plan(text)
        latest = {}
        for row in read_ledger(self.engine.base_dir):
            if row.get("checkpoint_id") == "final":
                latest[row.get("check_key") or row.get("item_text")] = row
        failed = {
            ci.parent_key
            for ci in snapshot.check_items
            if ci.kind == "verify"
            and ci.parent_key
            and latest.get(ci.key, {}).get("status") == "failed"
        }
        case_checks = [
            ci
            for ci in snapshot.check_items
            if ci.kind == "verify"
            and ci.parent_key is None
            and latest.get(ci.key, {}).get("status") == "failed"
        ]
        if case_checks and not failed and not any(i.is_active for i in snapshot.top_level):
            # A task-level criterion has no owning milestone. Reopen a single
            # role as the repair coordinator; the Operator may plan subsequent
            # role-bound milestones if the other device must act.
            role_hint = latest[case_checks[0].key].get("role_id")
            candidates = [i for i in snapshot.top_level if i.is_done]
            anchor = next(
                (i for i in reversed(candidates) if role_of(i) == role_hint),
                candidates[-1] if candidates else None,
            )
            if anchor is not None:
                failed.add(anchor.key)
        lines = []
        active_seen = False
        for line in text.splitlines():
            match = CHECKBOX_LINE_RE.match(line)
            if match and not match.group("indent"):
                key = subgoal_hash(match.group("text").strip())
                if key in failed or match.group("status") == "/":
                    status = "/" if not active_seen else " "
                    active_seen = True
                    import re

                    line = re.sub(r"^(\s*-\s*\[).(\])", lambda m: m[1] + status + m[2], line)
            lines.append(line)
        after = "\n".join(lines)
        self.commit_plan(self.plan_path, text, after, ExecutionScope(phase="final"))
        if self.policy.target(after) is None:
            raise RuntimeError(
                "Final review requested a retry without identifying a role-bound milestone"
            )
        self.final_retry_role = role_of(self.policy.target(after))
        logger.info(
            f"[multi-device] final.retry run_id={self.run_id} role_id={self.final_retry_role} failed_milestones={sorted(failed)} revision={self.plan_revision}"
        )

    async def run_checkpoint(self, role_id, callback, *, step_id=None, subgoal_hash=None):
        from uuid import UUID

        step = UUID(str(step_id)) if step_id else None
        with execution_scope(
            self.scope(phase="checkpoint", role_id=role_id, step_id=step, subgoal_hash=subgoal_hash)
        ):
            return await callback(self.contexts[role_id])

    async def final_observations(self):
        """Explicitly labelled observations; no implicit 'last device' at final review."""
        from artemis.controllers.unified_controller import UnifiedMobileController

        content = []
        for role_id, ctx in self.contexts.items():
            try:
                with execution_scope(self.scope(phase="checkpoint", role_id=role_id)):
                    screenshot = await asyncio.wait_for(
                        UnifiedMobileController(ctx).take_screenshot(), 20
                    )
                content.extend(
                    [
                        {
                            "type": "text",
                            "text": f"Final screenshot: role {role_id}, device {ctx.device.device_id}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{screenshot}"},
                        },
                    ]
                )
            except Exception as exc:
                content.append(
                    {"type": "text", "text": f"Final observation unavailable for {role_id}: {exc}"}
                )
        return content

    def save_manifest(self, request):
        data = {
            "role_plan": self.role_plan.model_dump(),
            "device_bindings": {k: v.model_dump() for k, v in request.role_bindings.items()},
            "app_profile": request.app_profile.model_dump() if request.app_profile else None,
            "feature_catalog": request.feature_catalog,
            "correlation_token": self.correlation_token,
        }
        path = self.engine.base_dir / "multi_device.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.engine.update_session_device_info(profile="pro", multi_device=True, **data)

    def save_progress(self, status):
        from artemis.runtime.multi_device.jobs import write_json

        write_json(
            self.engine.base_dir / "case_progress.json",
            {
                "status": status,
                "active_role": self.active_role,
                "active_milestone": self.origin_hash,
                "turns": self.turns,
                "plan_revision": self.plan_revision,
            },
        )
