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

"""In-process MCP session over the unified action server.

The in-memory transport (``create_connected_server_and_client_session``) wraps the
server in an ``anyio`` task group whose cancel scope must be entered and exited **in
the same task**. ``ActionSession`` therefore runs the whole context manager inside one
owner task and communicates through events: ``call()`` and ``aclose()`` are safe from
any task, which is what lets the session be created lazily inside an agent node and
torn down from ``ArtemisContext.__aexit__``.

Transport operations additionally run **only in the owner task**, via a request
queue: callers enqueue ``(future, name, args, timeout)`` and await the future. A
caller that times out or is cancelled only abandons its future -- the underlying
``call_tool`` is never cancelled mid-flight. This matters because cancelling
``call_tool`` on the in-memory transport corrupts the shared memory streams and
bricks the session for every subsequent caller (observed live: one 1-second
``asyncio.wait_for`` around a slow hierarchy fetch killed a 24-minute run, with
every later call failing on an empty-message ``BrokenResourceError``).

MCP's native ``read_timeout_seconds`` is just as fatal here (also observed live):
mcp 1.29's ``_handle_response`` delivers a LATE response into the timed-out
request's already-closed response stream, which crashes the client receive loop.
Per-call timeouts therefore travel as a ``timeout_ms`` tool argument and fire
inside the server tool, around the actuator call only -- a timed-out operation
produces a normal error payload and never a late response.

If the transport does die despite all this, the owner loop exits, which flips
``started`` to False so the next ``get_action_session`` builds a fresh session
instead of leaving the rest of the run to fail on a corpse.

The single owner loop also serializes calls, which enforces ARTEMIS's
single-device policy at the one choke point all agents share -- in particular it
stops nested device flows (e.g. Validator and a sub-agent) from interleaving two device
actions.
"""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from artemis.mcp.action_types import ActionResult, ObserveResult
from artemis.runtime.action_bindings import (
    ActionIdentity,
    CURRENT_ACTION,
    new_action_id,
    resolve_arguments,
    safe_symbolic_arguments,
    symbolic_arguments,
)
from artemis.runtime.value_bindings import BindingError, ValueBindings
from artemis.runtime.execution_scope import execution_scope
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["ActionSession", "get_action_session"]

_SHUTDOWN = object()  # queue sentinel


class ActionSession:
    """Owns the in-memory MCP client/server pair for one device run."""

    def __init__(self, server: Any):
        self._server = server
        self._session: Any = None
        self._ready = asyncio.Event()
        self._owner: asyncio.Task | None = None
        self._error: BaseException | None = None
        self._requests: asyncio.Queue = asyncio.Queue()

    async def _run(self) -> None:
        try:
            # Enter and exit strictly within this task (anyio cancel-scope rule).
            async with create_connected_server_and_client_session(self._server) as session:
                self._session = session
                self._ready.set()
                await self._serve(session)
        except BaseException as e:
            self._error = e
            self._ready.set()
            raise
        finally:
            self._session = None
            self._fail_pending("ActionSession owner task has exited.")

    async def _serve(self, session: Any) -> None:
        """Executes queued transport calls until the shutdown sentinel arrives."""
        while True:
            item = await self._requests.get()
            if item is _SHUTDOWN:
                return
            fut, name, args, scope, identity = item
            if fut.cancelled():
                continue  # caller gave up before we started; nothing was sent
            protected = False
            status = "failed"
            ctx = getattr(self, "device_context", None)
            require_refs = bool(getattr(ctx, "business_bindings_required", False))
            bindings = None
            if identity is not None:
                with execution_scope(scope) if scope is not None else nullcontext():
                    logger.debug(
                        "[bindings] dispatch.started action_id={} attempt={} role_id={}".format(
                            identity.action_id,
                            identity.attempt,
                            getattr(ctx, "role_id", None),
                        )
                    )
            try:
                with execution_scope(scope) if scope is not None else nullcontext():
                    ctx = getattr(self, "device_context", None)
                    runtime = getattr(ctx, "multi_device_runtime", None)
                    if runtime is not None:
                        runtime.require_device_scope(ctx.role_id)
                    # Preparation uses trusted app configuration. Business inputs
                    # emitted by model tools must remain symbolic through dispatch.
                    if scope is not None and scope.phase == "preparation":
                        require_refs = False
                    if require_refs:
                        bindings = getattr(
                            getattr(ctx, "data_engine", None), "value_bindings", None
                        )
                        if not isinstance(bindings, ValueBindings):
                            raise BindingError("Shared case value bindings are unavailable")
                    else:
                        bindings = getattr(ctx, "value_bindings", None)
                        if not isinstance(bindings, ValueBindings):
                            bindings = ValueBindings()
                    resolved, protected = resolve_arguments(
                        name,
                        args,
                        bindings,
                        getattr(ctx, "role_id", None),
                        require_refs=require_refs,
                    )
                    # References are revalidated after queueing, immediately before dispatch.
                    protected = protected or any(
                        isinstance(v, dict) and "ref" in v for v in args.values()
                    )
                    result = await session.call_tool(name, resolved)
                    if protected:
                        from mcp.types import TextContent

                        data = dict(result.structuredContent or {})
                        if "message" in data:
                            data["message"] = (
                                "Bound action dispatched"
                                if data.get("ok")
                                else "Bound action failed"
                            )
                            data["detail"] = None
                        result = result.model_copy(
                            update={
                                "content": [
                                    TextContent(type="text", text="Bound action completed")
                                ],
                                "structuredContent": data,
                            }
                        )
                    data = result.structuredContent or {}
                    status = "failed" if result.isError or data.get("ok") is False else "success"
            except BaseException as e:
                if identity is not None:
                    with execution_scope(scope) if scope is not None else nullcontext():
                        logger.warning(
                            "[bindings] dispatch.failed action_id={} attempt={} role_id={} error_type={} reason={}".format(
                                identity.action_id,
                                identity.attempt,
                                getattr(ctx, "role_id", None),
                                type(e).__name__,
                                str(e)
                                if isinstance(e, BindingError)
                                else "transport_or_scope_failure",
                            )
                        )
                if not fut.cancelled():
                    fut.set_exception(
                        BindingError("Bound action transport failed")
                        if protected and not isinstance(e, (BindingError, asyncio.CancelledError))
                        else e
                    )
                if isinstance(e, asyncio.CancelledError):
                    raise
                if isinstance(e, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
                    # The transport is dead; no future call on this session can
                    # succeed. Exit so `started` flips False and the next
                    # get_action_session() builds a fresh session.
                    logger.error(
                        f"Action session transport failed; retiring this session ({type(e).__name__})."
                    )
                    return
            finally:
                engine = getattr(ctx, "data_engine", None)
                recorder = getattr(engine, "record_action_binding", None)
                if identity is not None and callable(recorder):
                    recorded = False
                    try:
                        with execution_scope(scope) if scope is not None else nullcontext():
                            recorder(
                                action_id=identity.action_id,
                                attempt=identity.attempt,
                                name=name,
                                arguments=safe_symbolic_arguments(
                                    name, args, bindings, require_refs=require_refs
                                ),
                                status=status,
                                step_id=scope.step_id
                                if scope
                                else getattr(engine, "current_step_id", None),
                            )
                        recorded = True
                    finally:
                        if not recorded and not fut.done():
                            fut.set_exception(BindingError("Action recording failed"))
                if not fut.done():
                    fut.set_result(result)
                if identity is not None:
                    with execution_scope(scope) if scope is not None else nullcontext():
                        logger.debug(
                            "[bindings] dispatch.finished action_id={} attempt={} role_id={} status={} protected={}".format(
                                identity.action_id,
                                identity.attempt,
                                getattr(ctx, "role_id", None),
                                status,
                                protected,
                            )
                        )

    def _fail_pending(self, reason: str) -> None:
        while not self._requests.empty():
            item = self._requests.get_nowait()
            if item is _SHUTDOWN:
                continue
            fut = item[0]
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

    async def start(self) -> "ActionSession":
        if self._owner is not None:
            return self
        self._owner = asyncio.create_task(self._run(), name="artemis-action-session")
        await self._ready.wait()
        if self._error is not None:
            error = self._error
            self._owner = None
            self._error = None
            raise RuntimeError(f"Failed to start action session: {error}") from error
        return self

    @property
    def started(self) -> bool:
        return self._session is not None

    async def call_raw(self, name: str, args: dict[str, Any]) -> Any:
        """Calls a tool and returns the raw ``CallToolResult``.

        There is deliberately no transport-level timeout here (see the module
        docstring). Tools that support one take a ``timeout_ms`` argument and time
        out server-side. Wrapping this coroutine in ``asyncio.wait_for`` merely
        abandons the caller's future -- harmless, because the transport call itself
        runs in the owner task and cannot be cancelled from here.
        """
        ctx = getattr(self, "device_context", None)
        runtime = getattr(ctx, "multi_device_runtime", None)
        if runtime is not None:
            runtime.require_device_scope(ctx.role_id)
        if self._session is None:
            raise RuntimeError("ActionSession is not started.")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        from artemis.runtime.execution_scope import CURRENT_EXECUTION
        from artemis.mcp.action_manifest import INTERNAL_ACTIONS

        identity = None
        if name not in INTERNAL_ACTIONS:
            identity = CURRENT_ACTION.get() or ActionIdentity(new_action_id())
        self._requests.put_nowait(
            (fut, name, deepcopy(symbolic_arguments(args)), CURRENT_EXECUTION.get(), identity)
        )
        result = await fut
        if getattr(result, "isError", False):
            # Protocol-level failure (unknown tool, schema violation) -- a programming
            # error, not a device observation.
            text = ""
            for block in getattr(result, "content", []) or []:
                if getattr(block, "type", None) == "text":
                    text = block.text
                    break
            raise RuntimeError(f"MCP tool '{name}' failed: {text}")
        return result

    async def call(self, name: str, args: dict[str, Any]) -> ActionResult:
        """Calls a device action tool and returns its typed result."""
        result = await self.call_raw(name, args)
        return ActionResult.model_validate(result.structuredContent)

    async def observe(self, settle_ms: int = 400, include_image: bool = False) -> ObserveResult:
        """Captures and indexes the current screen via the internal observe tool."""
        result = await self.call_raw(
            "observe_screen", {"settle_ms": settle_ms, "include_image": include_image}
        )
        return ObserveResult.model_validate(result.structuredContent)

    async def screenshot_b64(self, timeout: float | None = None) -> str:
        """Returns the current screen as a base64 string (no persistence)."""
        args = {"timeout_ms": int(timeout * 1000)} if timeout else {}
        result = await self.call_raw("take_screenshot", args)
        payload = result.structuredContent or {}
        # FastMCP wraps plain-dict returns as {"result": ...} only for non-dict types;
        # dicts pass through.
        data = payload.get("result", payload)
        if not data.get("ok"):
            raise RuntimeError(f"take_screenshot failed: {data.get('error')}")
        return data["image_b64"]

    async def ui_hierarchy(self, timeout: float | None = None) -> Any:
        """Returns the raw UI element hierarchy."""
        args = {"timeout_ms": int(timeout * 1000)} if timeout else {}
        result = await self.call_raw("get_ui_hierarchy", args)
        payload = result.structuredContent or {}
        data = payload.get("result", payload)
        if not data.get("ok"):
            raise RuntimeError(f"get_ui_hierarchy failed: {data.get('error')}")
        return data["elements"]

    async def aclose(self) -> None:
        """Shuts the session down; safe to call from any task."""
        owner = self._owner
        if owner is None:
            return
        self._requests.put_nowait(_SHUTDOWN)
        interrupted = False
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=5.0)
        except asyncio.CancelledError:
            interrupted = True
        except Exception as e:
            logger.warning(f"Action session owner task did not exit cleanly: {e!r}")
        finally:
            # The owner runs the MCP transport and its anyio context. A lease
            # cannot be released while it may still dispatch a device action.
            if not owner.done():
                owner.cancel()
            while not owner.done():
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError:
                    if not owner.done():
                        interrupted = True
                        owner.cancel()
                except Exception:
                    break
            if not owner.cancelled():
                error = owner.exception()
                if error is not None:
                    logger.warning(f"Action session owner task failed on close: {error!r}")
            self._owner = None
            self._ready = asyncio.Event()
        if interrupted:
            raise asyncio.CancelledError


async def get_action_session(ctx: Any, actuator: Any = None) -> ActionSession:
    """Returns the context's action session, creating and starting it on first use.

    Contract validation happens here (inside ``build_action_server``), so a backend
    that cannot satisfy the manifest fails before the first device interaction.
    """
    existing = getattr(ctx, "action_session", None)
    if existing is not None and existing.started:
        return existing
    if existing is not None:
        # A dead session still owns a task and memory streams: release them
        # before the replacement is built.
        try:
            await existing.aclose()
        except Exception:
            logger.debug("Closing a dead action session failed; replacing it anyway.")

    from artemis.mcp.action_server import build_action_server
    from artemis.mcp.actuators.adb import AdbActuator

    actuator = actuator or getattr(ctx, "actuator", None) or AdbActuator(ctx)
    server = build_action_server(actuator)
    session = ActionSession(server)
    session.device_context = ctx
    await session.start()
    try:
        ctx.action_session = session
    except Exception:
        # Contexts that forbid new attributes (strict mocks) can still use the session.
        logger.debug("Could not store action session on ctx; proceeding unattached.")
    return session
