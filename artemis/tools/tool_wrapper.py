# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections.abc import Callable
import functools
import inspect
import re
import time
from typing import Annotated, Any, Literal, get_args, get_origin
import uuid

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState

from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.data_engine import engine as engine_mod
from artemis.data_engine.trace import CURRENT_TRACE_ID, smart_serialize
from artemis.graph.state import State
from third_party.mobile_use.tools.tool_wrapper import CompositeToolWrapper, ToolWrapper

__all__ = [
    "CompositeToolWrapper",
    "ToolWrapper",
    "get_tool_result_content",
    "invoke_tool_with_injection",
    "resolve_image_carrier",
    "split_multimodal_result",
    "tool_result_messages",
]


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
async def invoke_tool_with_injection(
    tool: BaseTool,
    args: dict,
    tool_call_id: str,
    state: State | None = None,
    record_trace: bool = True,
) -> Any:
    """Invokes a LangChain tool, manually injecting arguments marked with

    InjectedState or InjectedToolCallId if calling the underlying function
    directly.
    Automatically records type='tool' trace in DataEngine.
    """
    final_args = dict(args)

    def _real_callable(candidate: Any) -> Callable | None:
        """Return a routine or partial whose signature supports direct injection."""
        target = candidate.func if isinstance(candidate, functools.partial) else candidate
        if inspect.isroutine(target):
            return candidate
        return None

    # For StructuredTool, the original function might be in tool.func or tool.coroutine
    func = None
    if getattr(tool, "coroutine", None):
        func = _real_callable(tool.coroutine)
    if func is None and getattr(tool, "func", None):
        func = _real_callable(tool.func)

    if func:
        try:
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                annotation = param.annotation
                # Handle Annotated types
                if get_origin(annotation) is Annotated:
                    args_list = get_args(annotation)
                    if InjectedState in args_list:
                        final_args[param_name] = state
                    elif InjectedToolCallId in args_list:
                        final_args[param_name] = tool_call_id
        except (ValueError, TypeError):
            # If inspection fails, fall back to just using provided args
            pass

    else:
        tool_args = getattr(tool, "args", None)
        if state is not None and isinstance(tool_args, dict) and "state" in tool_args:
            final_args["state"] = state

    # The role guard is transparent: the guarded invocation owns the lifecycle.
    if (tool.metadata or {}).get("artemis_passthrough"):
        return await func(**final_args)

    from artemis.runtime.execution_scope import get_current_data_engine, CURRENT_EXECUTION
    from artemis.runtime.action_bindings import (
        ActionIdentity,
        CURRENT_ACTION,
        input_references,
        require_shell_allowed,
        resolve_arguments,
        safe_symbolic_arguments,
        tool_binding_contract,
    )
    from artemis.runtime.value_bindings import ValueBindings, BindingError
    from third_party.mobile_use.utils.logger import get_logger

    logger = get_logger(__name__)
    trace_id = uuid.uuid4()
    parent_id = CURRENT_TRACE_ID.get()
    token = CURRENT_TRACE_ID.set(trace_id)
    start_time = time.time()
    live_engine = get_current_data_engine()
    bindings = getattr(live_engine, "value_bindings", None)
    binding_engine = live_engine if isinstance(bindings, ValueBindings) else None
    scope = CURRENT_EXECUTION.get()
    require_refs = scope is not None and scope.runtime is not None
    role_id = scope.role_id if scope else None
    identity = ActionIdentity(str(trace_id))
    action_token = CURRENT_ACTION.set(identity)
    inputs, literals = {}, {}
    protected = False
    declared_action = bool((tool.metadata or {}).get("artemis_action"))
    execution_status, execution_result, execution_error = "running", None, None
    try:
        inputs, literals = tool_binding_contract(tool)
        if require_refs:
            require_shell_allowed(None, tool.name, args, binding_required=True)
        if require_refs and binding_engine is None:
            raise BindingError("Shared case value bindings are unavailable")
        if binding_engine is not None and (inputs or literals or declared_action):
            bindings.register_operation(tool.name, inputs=inputs, literals=literals)
        if record_trace and live_engine:
            trace_args = safe_symbolic_arguments(
                tool.name, args, bindings, require_refs=require_refs
            )
            live_engine.record_trace(
                type="tool",
                name=tool.name,
                payload={"args": {k: smart_serialize(v) for k, v in trace_args.items()}},
                status="running",
                parent_trace_id=parent_id,
                step_id=getattr(live_engine, "current_step_id", None),
                trace_id=trace_id,
            )
        if binding_engine is not None:
            resolved, protected = resolve_arguments(
                tool.name, args, bindings, role_id, require_refs=require_refs
            )
            final_args.update(resolved)
            identity = ActionIdentity(str(trace_id), input_refs=input_references(args))
            CURRENT_ACTION.set(identity)
        logger.debug(
            f"[bindings] tool.started action_id={identity.action_id} role_id={role_id} inputs={len(inputs)}"
        )
        if func is None:
            result = await tool.ainvoke(final_args)
        elif inspect.iscoroutinefunction(func):
            result = await func(**final_args)
        else:
            result = func(**final_args)
        execution_result = result
        execution_status = "failed" if is_tool_failure(result) else "success"
        if execution_status == "failed":
            execution_error = result
        outputs = bindings.outputs_for(identity.action_id) if binding_engine is not None else {}
        if outputs:
            if execution_status == "success":
                bindings.confirm_outputs(identity.action_id)
                result = {"outputs": {key: {"ref": value_id} for key, value_id in outputs.items()}}
            else:
                result = ToolFailure("Data-producing action failed")
                execution_error = result
            execution_result = result
        elif protected:
            result = (
                {"ok": True} if execution_status == "success" else ToolFailure("Bound tool failed")
            )
            execution_result = result
            if execution_status == "failed":
                execution_error = result
        return result
    except Exception as e:
        execution_status = "failed"
        execution_error = e
        logger.warning(
            f"[bindings] tool.failed action_id={identity.action_id} role_id={role_id} error_type={type(e).__name__}"
        )
        if protected or (binding_engine is not None and bindings.outputs_for(identity.action_id)):
            execution_error = BindingError("Data-producing or bound action failed")
            raise execution_error from None
        raise
    finally:
        duration = time.time() - start_time
        CURRENT_ACTION.reset(action_token)
        CURRENT_TRACE_ID.reset(token)
        if execution_status == "running":
            execution_status = "terminated"
        if binding_engine is not None:
            outputs = bindings.outputs_for(identity.action_id)
            direct_device_tool = tool.name.split(":")[-1] in {"run_adb_command", "manage_task"}
            if outputs or direct_device_tool or declared_action or inputs:
                if outputs and bindings.operation(tool.name) is None:
                    bindings.register_operation(tool.name, inputs=inputs, literals=literals)
                if execution_status != "success":
                    bindings.invalidate(list(outputs.values()))
                binding_engine.record_action_binding(
                    action_id=identity.action_id,
                    attempt=1,
                    name=tool.name,
                    arguments=safe_symbolic_arguments(
                        tool.name, args, bindings, require_refs=require_refs
                    ),
                    outputs=outputs,
                    status=execution_status,
                )
        if record_trace and live_engine:
            trace_args = safe_symbolic_arguments(
                tool.name, args, bindings, require_refs=require_refs
            )
            payload = {"args": {k: smart_serialize(v) for k, v in trace_args.items()}}
            if execution_status == "success":
                payload["result"] = smart_serialize(get_tool_result_content(execution_result))
            elif execution_status == "failed":
                payload["error"] = str(execution_error)
            else:
                payload["error"] = "Execution terminated unexpectedly."
            live_engine.record_trace(
                type="tool",
                name=tool.name,
                payload=payload,
                status=execution_status,
                duration=duration,
                parent_trace_id=parent_id,
                step_id=getattr(live_engine, "current_step_id", None),
                trace_id=trace_id,
            )
        logger.debug(
            f"[bindings] tool.finished action_id={identity.action_id} role_id={role_id} status={execution_status} duration_ms={int(duration * 1000)}"
        )


def get_tool_result_content(result: Any) -> Any:
    """Extracts the content from a tool result, handling ToolMessage returns."""
    if isinstance(result, ToolMessage):
        return result.content
    return result


# --- Multimodal tool results ------------------------------------------------------------

ImageCarrier = Literal["tool", "human"]

#: Providers whose tool-result messages may carry image parts directly.
_TOOL_IMAGE_PROVIDERS = frozenset({"google", "anthropic"})

_STEP_RE = re.compile(r"\bstep\s+(\d+)", re.IGNORECASE)


def split_multimodal_result(result: Any) -> tuple[str, list[dict[str, Any]]]:
    """Splits a tool result into its text and its image content blocks.

    Plain strings have no images; content-block lists contribute their text
    blocks (joined by newlines) and their ``image_url`` / ``image`` blocks.
    ``ToolMessage`` results are unwrapped first.
    """
    content = get_tool_result_content(result)
    if content is None:
        return "", []
    if isinstance(content, list):
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                images.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t), images
    return content if isinstance(content, str) else str(content), []


def resolve_image_carrier(llm_or_provider: Any) -> ImageCarrier:
    """Which message carries images returned by a tool for this LLM.

    Gemini (direct API) and Anthropic accept image parts inside the tool
    result message itself (``"tool"``). Vertex AI and the OpenAI-compatible
    providers (openai / openrouter / xai / ...) reject them there, so the
    image travels in a ``HumanMessage`` that immediately follows the textual
    tool result (``"human"``). Accepts a provider name, a provider enum, or an
    LLM whose ``endpoint.provider`` names the provider that actually issues
    the request (a fallback model is judged by its own provider).
    """
    provider: Any = llm_or_provider
    if provider is not None and not isinstance(provider, str):
        # An LLM (RobustChatModelWrapper) names its provider on ``endpoint``;
        # anything without an endpoint is taken as the provider itself.
        endpoint = getattr(provider, "endpoint", None)
        if endpoint is not None:
            provider = getattr(endpoint, "provider", None)
    value = str(getattr(provider, "value", provider) or "").lower()
    return "tool" if value in _TOOL_IMAGE_PROVIDERS else "human"


def tool_result_messages(
    tool_call_id: str,
    result: Any,
    *,
    name: str | None = None,
    status: str = "success",
    image_carrier: ImageCarrier | None = None,
    llm: Any = None,
) -> list[BaseMessage]:
    """The conversation messages that deliver one tool result to the model.

    Text-only results become a single ``ToolMessage``. Results carrying images
    (e.g. ``get_step_screenshot``) are delivered according to the image
    carrier — given explicitly or resolved from ``llm`` — so every agent loop
    handles multimodal tool results the same way and no loop ever pastes
    base64 into a text field. The tool call id, name and status are preserved.
    """
    text, images = split_multimodal_result(result)
    extra = {"name": name} if name else {}
    if not images:
        content = get_tool_result_content(result)
        if not isinstance(content, (str, list)):
            content = text
        return [ToolMessage(tool_call_id=tool_call_id, content=content, status=status, **extra)]

    carrier = image_carrier or resolve_image_carrier(llm)
    if carrier == "tool":
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        return [
            ToolMessage(
                tool_call_id=tool_call_id, content=[*blocks, *images], status=status, **extra
            )
        ]

    match = _STEP_RE.search(text)
    caption = f"[Screenshot returned by {name or 'the tool'}"
    if match:
        caption += f" for step {match.group(1)}"
    caption += "]"
    return [
        ToolMessage(tool_call_id=tool_call_id, content=text, status=status, **extra),
        HumanMessage(content=[{"type": "text", "text": caption}, *images]),
    ]
