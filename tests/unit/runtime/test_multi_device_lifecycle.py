"""Hardware-free tests of actual SDK resource ownership and worker contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from artemis.context import DeviceContext
from artemis.runtime.multi_device.models import AppProfile, MultiDeviceRequest, RolePlan
from artemis.sdk.builders import Builders


@pytest.fixture
def runner_environment(tmp_path, monkeypatch):
    import artemis.sdk.multi_device_agent as module

    monkeypatch.setattr(module.settings, "TRACES_PATH", tmp_path)
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path / "locks")
    cleaned, initialized, engines = [], [], []

    class FakeAgent:
        fail_device = None

        def __init__(self, *, config, device_serial, session_id):
            self.serial = device_serial
            self._device_context = DeviceContext(device_id=device_serial)
            self._adb_client = None
            self._ui_adb_client = None

        async def init(self):
            initialized.append(self.serial)
            if self.serial == self.fail_device:
                raise RuntimeError("device disconnected")

        async def clean(self, **_):
            cleaned.append(self.serial)

        _connect_screen_client = AsyncMock()
        _ensure_device_unlocked = AsyncMock()
        _prepare_device_environment = AsyncMock()

    class FakeEngine:
        def __init__(self, ctx):
            self.base_dir = tmp_path
            self.shutdowns = 0
            engines.append(self)

        def start_session(self, case, info, run_id):
            self.base_dir = tmp_path / run_id
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.current_session_id = run_id

        def update_session_device_info(self, **_):
            pass

        def _publish(self, *_):
            pass

        def end_session(self, status):
            self.status = status

        async def shutdown(self):
            self.shutdowns += 1

    class FakeController:
        def __init__(self, ctx):
            pass

        async def take_screenshot(self):
            return "aW1hZ2U="

    monkeypatch.setattr(module, "Agent", FakeAgent)
    monkeypatch.setattr(module, "RoleDataEngine", FakeEngine)
    monkeypatch.setattr(module, "UnifiedMobileController", FakeController)
    plan = RolePlan(roles=[{"id": "a", "case_actor": "A"}, {"id": "b", "case_actor": "B"}])
    monkeypatch.setattr(module, "plan_roles", AsyncMock(return_value=plan))
    config = Builders.AgentConfig.with_checker(enabled=False).build(validate_profiles=False)
    spec = MultiDeviceRequest(
        case="A sends, B replies",
        record_video=False,
        role_bindings={"a": {"serial": "device-a"}, "b": {"serial": "device-b"}},
    )
    return SimpleNamespace(
        module=module,
        config=config,
        spec=spec,
        plan=plan,
        cleaned=cleaned,
        initialized=initialized,
        engines=engines,
        fake_agent=FakeAgent,
    )


@pytest.mark.asyncio
async def test_preplanned_sdk_case_does_not_plan_roles_again(runner_environment, monkeypatch):
    env = runner_environment
    env.module.plan_roles.side_effect = AssertionError("RolePlan generated twice")

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text(
                "- [x] [role:a] Send\n- [x] [role:b] Receive", encoding="utf-8"
            )
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(
        env.spec, role_plan=env.plan
    )
    assert result["status"] == "completed", result
    env.module.plan_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_preplanned_role_plan_fails_before_device_access(runner_environment):
    env = runner_environment
    wrong = RolePlan(roles=[{"id": "a", "case_actor": "A"}])
    with pytest.raises(ValueError, match="Required role bindings"):
        await env.module.MultiDeviceAgent(config=env.config).run_case(
            env.spec, role_plan=wrong
        )
    assert env.initialized == []


@pytest.mark.asyncio
async def test_second_device_init_failure_releases_everything(runner_environment):
    env = runner_environment
    env.fake_agent.fail_device = "device-b"
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] == "failed" and "disconnected" in result["error"]
    assert env.cleaned == ["device-a", "device-b"]
    assert env.engines[0].shutdowns == 1
    from artemis.runtime.device_lock import DeviceExecutionLock

    assert not DeviceExecutionLock.get_active_owners()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete,expected", [(True, "completed"), (False, "failed")])
async def test_sdk_completion_requires_finished_milestones(
    runner_environment, monkeypatch, complete, expected
):
    env = runner_environment

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text(
                f"- [{'x' if complete else ' '}] [role:a] Send", encoding="utf-8"
            )
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] == expected
    assert env.cleaned == ["device-a", "device-b"]
    assert env.engines[0].shutdowns == 1
    from pathlib import Path

    stored = json.loads(
        (Path(result["trace_path"]) / "case_result.json").read_text(encoding="utf-8")
    )
    assert stored["status"] == expected


@pytest.mark.asyncio
async def test_sdk_records_custom_trace_directory_and_test_name(
    runner_environment, monkeypatch, tmp_path
):
    env = runner_environment
    trace_root = tmp_path / "custom-traces"
    original_engine = env.module.RoleDataEngine

    class TraceEngine(original_engine):
        def __init__(self, ctx):
            super().__init__(ctx)
            self.trace_name = ctx.execution_setup.trace_name
            self.trace_root = ctx.execution_setup.traces_path

        def start_session(self, case, info, run_id):
            self.info = info
            self.base_dir = self.trace_root / str(run_id)
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.current_session_id = run_id

    monkeypatch.setattr(env.module, "RoleDataEngine", TraceEngine)

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text("- [x] [role:a] Done", encoding="utf-8")
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(
        config=env.config, test_name="meeting-case", traces_path=trace_root
    ).run_case(env.spec)
    assert result["status"] == "completed"
    assert result["test_name"] == "meeting-case"
    assert result["trace_path"] == str(trace_root / result["run_id"])
    assert env.engines[0].trace_name == "meeting-case"
    assert env.engines[0].info["test_name"] == "meeting-case"
    stored = json.loads((trace_root / result["run_id"] / "case_result.json").read_text())
    assert stored["test_name"] == "meeting-case"


@pytest.mark.asyncio
async def test_apk_without_package_is_installed_on_each_role(runner_environment, monkeypatch):
    env = runner_environment
    installs = []

    async def install(agent, path):
        installs.append((agent.serial, path))

    env.fake_agent.install_apk = install
    env.spec.apk_path = "fixture.apk"

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text("- [x] [role:a] Done", encoding="utf-8")
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] == "completed"
    assert installs == [("device-a", "fixture.apk"), ("device-b", "fixture.apk")]


@pytest.mark.asyncio
async def test_app_profile_package_is_prepared_without_case_literal(
    runner_environment, monkeypatch
):
    env = runner_environment
    env.spec.case = "A logs in; B logs in"
    env.spec.app_profile = AppProfile(package="com.example.meeting")
    seen = []

    async def launch(ctx, package):
        assert package not in env.spec.case
        assert package == env.spec.app_profile.package
        seen.append(ctx.role_id)
        return True, None

    monkeypatch.setattr(
        "third_party.mobile_use.utils.app_launch_utils.launch_app_with_retries", launch
    )

    async def graph(runtime):
        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text("- [x] [role:a] Done", encoding="utf-8")
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] == "completed"
    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_cancellation_waits_for_resource_release(runner_environment, monkeypatch):
    env = runner_environment
    entered = asyncio.Event()

    async def graph(runtime):
        async def invoke(*_, **__):
            entered.set()
            await asyncio.Event().wait()

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    runner = env.module.MultiDeviceAgent(config=env.config, session_id=str(uuid4()))
    task = asyncio.create_task(runner.run_case(env.spec))
    await asyncio.wait_for(entered.wait(), 3)
    runner.stop_current_task()
    runner.stop_current_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.cleaned == ["device-a", "device-b"]
    assert env.engines[0].shutdowns == 1
    result = json.loads((env.engines[0].base_dir / "case_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_trace_shutdown_and_result_write(
    runner_environment, monkeypatch
):
    env = runner_environment
    graph_entered, shutdown_entered, release_shutdown = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_engine = env.module.RoleDataEngine

    class SlowEngine(original_engine):
        async def shutdown(self):
            shutdown_entered.set()
            await release_shutdown.wait()
            self.shutdowns += 1

    async def graph(runtime):
        async def invoke(*_, **__):
            graph_entered.set()
            await asyncio.Event().wait()

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "RoleDataEngine", SlowEngine)
    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    runner = env.module.MultiDeviceAgent(config=env.config)
    task = asyncio.create_task(runner.run_case(env.spec))
    await asyncio.wait_for(graph_entered.wait(), 3)
    runner.stop_current_task()
    await asyncio.wait_for(shutdown_entered.wait(), 3)
    runner._task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.engines[0].shutdowns == 1
    stored = json.loads((env.engines[0].base_dir / "case_result.json").read_text())
    assert stored["status"] == "cancelled"


def test_all_or_release_lease_does_not_hold_first_device(tmp_path, monkeypatch):
    from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
    from artemis.runtime.multi_device.leases import DeviceSetLease

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path)
    owner = DeviceExecutionLock("device-b", session_id="other")
    owner.acquire()
    try:
        lease = DeviceSetLease(["device-a", "device-b"], "case", timeout=0)
        with pytest.raises(DeviceBusyError):
            lease.acquire()
        assert lease.held == []
        independent = DeviceExecutionLock("device-a", session_id="probe")
        independent.acquire(blocking=False)
        independent.release()
    finally:
        owner.release()


@pytest.mark.asyncio
async def test_unprepared_role_requirements_fail_before_reserving_devices(runner_environment):
    env = runner_environment
    env.plan.roles[0].feature_requirements = {"unknown": True}
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    assert result["status"] == "failed"
    assert "Feature requirements are not prepared" in result["error"]
    assert env.initialized == []


def test_multiple_devices_count_as_one_case_for_global_limit(tmp_path, monkeypatch):
    from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
    from artemis.runtime.multi_device.leases import DeviceSetLease

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: tmp_path)
    lease = DeviceSetLease(["a", "b", "c"], "case-one", timeout=0, max_concurrency=2)
    other = DeviceExecutionLock("d", session_id="case-two", max_concurrency=2)
    third = DeviceExecutionLock("e", session_id="case-three", max_concurrency=2)
    lease.acquire()
    try:
        other.acquire(blocking=False)
        try:
            with pytest.raises(DeviceBusyError, match="concurrency limit"):
                third.acquire(blocking=False)
        finally:
            other.release()
    finally:
        lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [None, {"member": False}, {"member": True}])
async def test_feature_requirements_follow_prepared_device_features(
    runner_environment, monkeypatch, prepared
):
    env = runner_environment
    env.spec.feature_catalog = {"member": "Account membership state"}
    env.plan.roles[0].feature_requirements = {"member": True}
    if prepared is not None:
        env.spec.role_bindings["a"].prepared_features = prepared
    graph_calls = []

    async def graph(runtime):
        graph_calls.append(True)

        async def invoke(*_, **__):
            runtime.plan_path.parent.mkdir(parents=True, exist_ok=True)
            runtime.plan_path.write_text("- [x] [role:a] Done", encoding="utf-8")
            return {"run_outcome": {"task_status": "completed", "tests": {}}}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(env.module, "get_multi_device_graph", graph)
    result = await env.module.MultiDeviceAgent(config=env.config).run_case(env.spec)
    success = prepared == {"member": True}
    assert result["status"] == ("completed" if success else "failed"), result
    assert len(graph_calls) == int(success)
    if success:
        assert env.engines[0].shutdowns == 1
    else:
        assert env.initialized == []
