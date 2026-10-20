"""Managed graph boundaries; native node algorithms stay unchanged."""

from dataclasses import dataclass, replace
import inspect

from artemis.graph.state import State
from artemis.runtime.execution_scope import CURRENT_EXECUTION, execution_scope


@dataclass
class TurnOutcome:
    """Only completed nodes may associate evidence with the staged turn."""

    step_id: str | None = None
    validator_result: dict | None = None


def scope_graph_nodes(builder, ctx):
    add_native_node = builder.add_node

    def add_scoped_node(node, action=None, **options):
        name = node if isinstance(node, str) else getattr(node, "__name__", "node")
        action = action if action is not None else node

        async def invoke(state: State):
            control = ctx.task_control
            if name == "operator":
                control.turn_outcome = TurnOutcome()
            scope = replace(ctx.execution_scope, phase=name, step_id=control.step_id)
            with execution_scope(scope):
                result = action(state)
                result = await result if inspect.isawaitable(result) else result
                # LangGraph creates separate Tasks; cursor changes do not flow
                # back through ContextVars. Children retain their captured cursor.
                control.step_id = CURRENT_EXECUTION.get().step_id
                turn = control.turn_outcome
                if turn is not None and isinstance(result, dict):
                    if name == "execution_check":
                        turn.step_id = result.get("current_step_id")
                    elif name == "validator" and turn.step_id == state.current_step_id:
                        turn.validator_result = result.get("last_execution_result")
                return result

        return add_native_node(name, invoke, **options)

    builder.add_node = add_scoped_node


def harvest_task_checkpoints(ctx, state):
    """Book all stopped producers, including completed-but-unharvested checks."""
    from artemis.graph.checkpoints import harvest_run

    for cid, run in list(ctx.checkpoint_tasks.items()):
        if not run.task.done():
            raise RuntimeError("Checkpoint still owns task resources")
        harvest_run(ctx, state, run, allow_side_effects=False, cancelled_status="cancelled")
        del ctx.checkpoint_tasks[cid]
    ctx.pending_checkpoints.clear()
