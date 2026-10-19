from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.runtime.multi_device.models import MultiDeviceRequest, RolePlan


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 3])
async def test_case_plans_count_before_any_device_binding(monkeypatch, count):
    import artemis.agents.role_planner as planner
    import artemis.runtime.multi_device.allocation as allocation

    plan = RolePlan(roles=[{"id": chr(97 + i), "case_actor": chr(65 + i)} for i in range(count)])
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan)
    monkeypatch.setattr(planner, "get_llm", lambda **_: llm)
    request = MultiDeviceRequest(case="Case determines its own participants")
    assert request.role_bindings == {}
    actual = await planner.plan_roles(SimpleNamespace(), request)
    devices = [
        SimpleNamespace(serial=f"phone-{i}", state="device", is_busy=False) for i in range(3)
    ]
    monkeypatch.setattr(
        allocation.device_pool, "list_devices_async", AsyncMock(return_value=devices)
    )
    bindings = await allocation.allocate_roles(actual)
    assert len(bindings) == count
    assert len({d.serial for d in bindings.values()}) == count


@pytest.mark.asyncio
async def test_not_enough_devices_fails_before_operating(monkeypatch):
    import artemis.runtime.multi_device.allocation as allocation

    monkeypatch.setattr(
        allocation.device_pool,
        "list_devices_async",
        AsyncMock(
            return_value=[SimpleNamespace(serial="only-phone", state="device", is_busy=False)]
        ),
    )
    plan = RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])
    with pytest.raises(ValueError, match="requires 2 distinct devices"):
        await allocation.allocate_roles(plan)


@pytest.mark.asyncio
async def test_original_pro_cli_dispatches_multi_role_before_device_selection(monkeypatch):
    import importlib

    cli = importlib.import_module("artemis.interfaces.cli.commands.run")
    import artemis.runtime.multi_device.dispatch as dispatch

    builder = MagicMock()
    monkeypatch.setattr(cli, "new_default_config_builder", lambda: builder)
    monkeypatch.setattr(cli, "publish_startup_progress", lambda *a, **kw: None)
    monkeypatch.delenv("ARTEMIS_CLOUD_MODE", raising=False)
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(dispatch, "dispatch_multi_role_if_needed", route)
    single = AsyncMock()
    monkeypatch.setattr(cli, "run_automation", single)
    await cli.execute_task("A calculates, B uses result, A finishes", profile="pro")
    request = route.call_args.args[1]
    assert request.role_bindings == {}
    assert request.case == "A calculates, B uses result, A finishes"
    single.assert_not_awaited()
    builder.for_device.assert_not_called()


@pytest.mark.asyncio
async def test_pro_cli_preserves_native_single_role_parameters(monkeypatch, tmp_path):
    import importlib

    cli = importlib.import_module("artemis.interfaces.cli.commands.run")
    import artemis.runtime.multi_device.dispatch as dispatch

    builder = MagicMock()
    monkeypatch.setattr(cli, "new_default_config_builder", lambda: builder)
    monkeypatch.setattr(cli, "publish_startup_progress", lambda *a, **kw: None)
    monkeypatch.delenv("ARTEMIS_CLOUD_MODE", raising=False)
    route = AsyncMock(return_value=False)
    monkeypatch.setattr(dispatch, "dispatch_multi_role_if_needed", route)
    native = AsyncMock()
    monkeypatch.setattr(cli, "run_automation", native)
    await cli.execute_task(
        "Run installed app",
        profile="pro",
        device_serial="device-a",
        test_name="custom-case",
        traces_output_path_str=str(tmp_path),
        app_path="fixture.apk",
    )
    assert route.call_args.kwargs["test_name"] == "custom-case"
    assert route.call_args.kwargs["traces_path"] == str(tmp_path)
    request = route.call_args.args[1]
    assert request.apk_path == "fixture.apk"
    assert request.app_profile is None
    builder.for_device.assert_called_once()
    native.assert_awaited_once()
    assert native.call_args.kwargs["profile"] == "pro"
    assert native.call_args.kwargs["app_path"] == "fixture.apk"


@pytest.mark.asyncio
async def test_dispatch_plans_once_and_passes_exact_role_plan_to_runner(monkeypatch):
    import artemis.runtime.multi_device.dispatch as dispatch

    plan = RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])
    planned = AsyncMock(return_value=plan)
    monkeypatch.setattr(dispatch, "plan_roles", planned)
    instance = SimpleNamespace(run_case=AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(dispatch, "MultiDeviceAgent", lambda **_: instance)
    profile = SimpleNamespace(llm_config=None)
    config = SimpleNamespace(agent_profiles={"pro": profile}, default_profile=profile)
    request = MultiDeviceRequest(case="A then B")
    assert await dispatch.dispatch_multi_role_if_needed(config, request)
    planned.assert_awaited_once()
    assert instance.run_case.call_args.kwargs["role_plan"] is plan


@pytest.mark.asyncio
async def test_dispatch_single_role_does_not_start_multi_runner(monkeypatch):
    import artemis.runtime.multi_device.dispatch as dispatch

    planned = AsyncMock(return_value=RolePlan(roles=[{"id": "a", "case_actor": "A"}]))
    monkeypatch.setattr(dispatch, "plan_roles", planned)
    constructor = MagicMock()
    monkeypatch.setattr(dispatch, "MultiDeviceAgent", constructor)
    profile = SimpleNamespace(llm_config=None)
    config = SimpleNamespace(agent_profiles={"pro": profile}, default_profile=profile)
    assert not await dispatch.dispatch_multi_role_if_needed(
        config, MultiDeviceRequest(case="One actor")
    )
    planned.assert_awaited_once()
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_role_planner_checks_explicit_catalog_values_but_keeps_descriptions(monkeypatch):
    import artemis.agents.role_planner as planner

    plan = RolePlan(
        roles=[{"id": "a", "case_actor": "A", "feature_requirements": {"tier": "gold"}}]
    )
    descriptive = MultiDeviceRequest(
        case="Account tier",
        feature_catalog={"tier": "Account membership tier"},
    )
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan)
    monkeypatch.setattr(planner, "get_llm", lambda **_: llm)
    assert await planner.plan_roles(SimpleNamespace(), descriptive) == plan
    enumerated = descriptive.model_copy(
        update={
            "feature_catalog": {"tier": {"description": "Membership", "allowed_values": ["silver"]}}
        }
    )
    with pytest.raises(ValueError, match="Unsupported value"):
        await planner.plan_roles(SimpleNamespace(), enumerated)
    enumerated.feature_catalog["tier"]["allowed_values"].append("gold")
    assert await planner.plan_roles(SimpleNamespace(), enumerated) == plan


@pytest.mark.asyncio
async def test_role_planner_repairs_one_contract_error_within_shared_stage(monkeypatch):
    import artemis.agents.role_planner as planner

    incomplete = RolePlan(roles=[{"id": "a", "case_actor": "A"}])
    complete = RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])
    model = MagicMock()
    model.endpoint.timeout_seconds = 10.0
    model.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=[
            {"raw": None, "parsed": incomplete, "parsing_error": None},
            {"raw": None, "parsed": complete, "parsing_error": None},
        ]
    )
    monkeypatch.setattr(planner, "get_llm", lambda **_: model)
    request = MultiDeviceRequest(
        case="A then B",
        role_bindings={"a": {"serial": "one"}, "b": {"serial": "two"}},
    )
    assert await planner.plan_roles(SimpleNamespace(), request) == complete
    assert model.with_structured_output.return_value.ainvoke.await_count == 2
    assert (
        "Required role bindings"
        in model.with_structured_output.return_value.ainvoke.call_args.args[0][-1].content
    )


@pytest.mark.asyncio
async def test_role_planner_stops_on_unresolved_without_repair(monkeypatch):
    import artemis.agents.role_planner as planner

    model = MagicMock()
    model.endpoint.timeout_seconds = 10.0
    model.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=RolePlan(
            roles=[{"id": "a", "case_actor": "A"}],
            unresolved=["Which account belongs to A?"],
        )
    )
    monkeypatch.setattr(planner, "get_llm", lambda **_: model)
    with pytest.raises(ValueError, match="needs input"):
        await planner.plan_roles(SimpleNamespace(), MultiDeviceRequest(case="A logs in"))
    assert model.with_structured_output.return_value.ainvoke.await_count == 1


def test_case_rejects_ambiguous_apk_sources():
    with pytest.raises(ValueError, match="either apk_path"):
        MultiDeviceRequest(
            case="Case",
            apk_path="first.apk",
            app_profile={"package": "com.example.app", "apk_path": "second.apk"},
        )


@pytest.mark.asyncio
async def test_pro_http_keeps_original_queue_and_defers_device_selection(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from apps.admin_console.routers import tasks

    app = FastAPI()
    app.include_router(tasks.router)
    probe = AsyncMock()
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", probe)
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/run", json={"goal": "A then B then A", "profile": "pro"})
    assert response.status_code == 200
    probe.assert_not_awaited()
    assert enqueue.call_args.kwargs["device_serial"] is None
    assert enqueue.call_args.kwargs["profile"] == "pro"


def test_inherited_queue_ticket_can_retry_whole_device_set(tmp_path, monkeypatch):
    import threading
    from artemis.runtime.device_lock import DeviceExecutionLock
    from artemis.runtime.multi_device.leases import DeviceSetLease

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path)
    owner = DeviceExecutionLock("b", session_id="other", max_concurrency=0)
    owner.acquire()
    ticket = DeviceExecutionLock.reserve("Pro queued case", "pending", session_id="case")
    monkeypatch.setenv(DeviceExecutionLock.QUEUE_TICKET_ENV, ticket)
    lease = DeviceSetLease(["a", "b"], "case", timeout=2, max_concurrency=0)
    timer = threading.Timer(0.1, owner.release)
    timer.start()
    try:
        lease.acquire()
        assert len(lease.held) == 2
    finally:
        timer.join()
        lease.release()
