"""Final visual review must not mix fresh pixels with stale accessibility text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.agents.checker import checker
from artemis.context import ArtemisContext, ExecutionSetup


def install_capture(monkeypatch, *, screenshot="NEW_25", error=None):
    capture = AsyncMock(side_effect=error, return_value=SimpleNamespace(
        base64=screenshot, width=100, height=200,
        elements=[{"text": "OLD_8", "bounds": "[0,0][100,100]"}],
    ))
    monkeypatch.setattr("artemis.controllers.unified_controller.UnifiedMobileController",
                        lambda ctx: SimpleNamespace(get_screen_data=capture))
    return capture


@pytest.mark.asyncio
async def test_final_capture_without_ocr_omits_stale_hierarchy(monkeypatch):
    install_capture(monkeypatch)
    monkeypatch.setattr(checker, "is_ocr_configured", lambda: False)
    image, text = await checker._capture_final_screen(object())
    assert image == "NEW_25"
    assert "OLD_8" not in text
    assert "hierarchy omitted" in text and "not guaranteed" in text


@pytest.mark.asyncio
async def test_final_capture_ocr_reads_exact_image_and_never_merges_old_xml(monkeypatch):
    install_capture(monkeypatch)
    monkeypatch.setattr(checker, "is_ocr_configured", lambda: True)
    ocr = AsyncMock(return_value=[{"text": "NEW_25", "position": [
        {"x": 10, "y": 20}, {"x": 90, "y": 20}, {"x": 90, "y": 50}, {"x": 10, "y": 50},
    ]}])
    monkeypatch.setattr(checker, "perform_ocr", ocr)
    image, text = await checker._capture_final_screen(object())
    ocr.assert_awaited_once_with("NEW_25")
    assert image == "NEW_25" and "NEW_25" in text
    assert "OLD_8" not in text


@pytest.mark.asyncio
async def test_final_capture_ocr_failure_preserves_pixels_without_old_text(monkeypatch):
    install_capture(monkeypatch)
    monkeypatch.setattr(checker, "is_ocr_configured", lambda: True)
    monkeypatch.setattr(checker, "perform_ocr", AsyncMock(side_effect=RuntimeError("OCR failed")))
    image, text = await checker._capture_final_screen(object())
    assert image == "NEW_25" and "OCR unavailable" in text
    assert "OLD_8" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("screenshot,error", [(None, None), ("", None), (None, RuntimeError("capture failed"))])
async def test_missing_final_pixels_never_fall_back_to_old_context(monkeypatch, screenshot, error):
    install_capture(monkeypatch, screenshot=screenshot, error=error)
    ctx = SimpleNamespace(latest_screenshot="OLD_8", latest_ui_hierarchy="OLD_8")
    image, text = await checker._capture_final_screen(ctx)
    assert image is None and "Final screenshot unavailable" in text
    assert "OLD_8" not in text


@pytest.mark.asyncio
async def test_final_review_prompt_retains_new_image_and_omits_old_xml(monkeypatch):
    install_capture(monkeypatch)
    monkeypatch.setattr(checker, "is_ocr_configured", lambda: False)
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup()
    ctx.data_engine = MagicMock(current_step_id=None)
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    captured = []
    async def review(ctx, messages, tools, items):
        captured.extend(messages)
        return checker.CheckReport()
    monkeypatch.setattr(checker, "_run_check_loop", review)
    await checker.run_final_check(ctx, "read actual result", "", [], [])
    assert "OLD_8" not in str(captured)
    content = captured[1].content
    assert any(block.get("type") == "image_url" and block["image_url"]["url"].endswith("NEW_25") for block in content)
    assert "hierarchy omitted" in content[0]["text"]
