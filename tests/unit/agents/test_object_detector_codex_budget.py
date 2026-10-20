"""CLI budget regression discovered in the two-phone calculator run."""
import asyncio
from types import SimpleNamespace
import pytest
from artemis.agents.object_detector import object_detector as detector


@pytest.mark.asyncio
async def test_codex_detector_uses_resolved_endpoint_budget(monkeypatch):
    observed = {}
    model = SimpleNamespace(endpoint=SimpleNamespace(provider="codex", timeout_seconds=300))
    monkeypatch.setattr(detector, "get_llm", lambda *a, **k:model)
    async def detect(*args, timeout_val, **kwargs):
        observed["call"] = timeout_val
        return [{"point":[500,500],"label":"clear"}]
    monkeypatch.setattr(detector,"_detect_single_label",detect)
    original_wait=asyncio.wait
    async def wait(tasks, *, timeout):
        observed["global"]=timeout
        return await original_wait(tasks,timeout=timeout)
    monkeypatch.setattr(detector.asyncio,"wait",wait)
    result=await detector._run_object_detection(SimpleNamespace(llm_config=None),b"image",["clear"])
    assert observed == {"call":300.0,"global":300.0}
    assert not result["failed"]


@pytest.mark.asyncio
async def test_explicit_global_deadline_cancels_and_drains_cli_detector(monkeypatch):
    stopped=asyncio.Event()
    model=SimpleNamespace(endpoint=SimpleNamespace(provider="codex",timeout_seconds=300))
    monkeypatch.setattr(detector,"get_llm",lambda *a,**k:model)
    async def detect(*args,**kwargs):
        try:await asyncio.Event().wait()
        finally:stopped.set()
    monkeypatch.setattr(detector,"_detect_single_label",detect)
    result=await detector._run_object_detection(SimpleNamespace(llm_config=None),b"image",["clear"],global_timeout=.01)
    assert stopped.is_set()
    assert result["failed"] == ["clear"]
