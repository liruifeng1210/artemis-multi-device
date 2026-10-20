"""Exercise Codex protocol translation without network or account usage."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel
import pytest

from artemis.llm.codex import CodexChatModel, _transcript


@pytest.fixture
def cli(monkeypatch):
    from artemis.llm import codex

    captured = {}

    async def start(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        process = AsyncMock()
        process.returncode = 0

        async def communicate(data=None):
            captured["prompt"] = data.decode("utf-8")
            out = args[args.index("--output-last-message") + 1]
            from pathlib import Path

            Path(out).write_text(json.dumps(captured["payload"]), encoding="utf-8")
            return b'{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":3}}\n', b""

        process.communicate.side_effect = communicate
        return process

    monkeypatch.setattr(codex, "find_codex", lambda: "codex.exe")
    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", start)
    captured["payload"] = {"content": "ok", "tool_calls": []}
    return captured


@pytest.mark.asyncio
async def test_text_cli_flags_and_usage(cli):
    result = await CodexChatModel().ainvoke("hello")
    assert result.content == "ok"
    assert result.usage_metadata["total_tokens"] == 8
    args = cli["args"]
    assert args[args.index("--model") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="xhigh"' in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in args and "--ephemeral" in args


@pytest.mark.asyncio
async def test_tool_roundtrip(cli):
    tool = {
        "name": "tap",
        "description": "Tap a coordinate",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    }
    cli["payload"] = {"content": "Tap", "tool_calls": [{"name": "tap", "arguments": '{"x":42}'}]}
    model = CodexChatModel().bind_tools([tool], tool_choice="required")
    result = await model.ainvoke([HumanMessage(content="Tap x=42")])
    assert result.tool_calls[0]["args"] == {"x": 42}
    assert model._schema()["properties"]["tool_calls"]["minItems"] == 1
    assert "Tap a coordinate" in cli["prompt"]


@pytest.mark.asyncio
async def test_structured_output(cli):
    class Answer(BaseModel):
        value: int

    cli["payload"] = {"content": "", "tool_calls": [{"name": "Answer", "arguments": '{"value":7}'}]}
    result = await CodexChatModel().with_structured_output(Answer).ainvoke("Return value 7")
    assert isinstance(result, Answer)
    assert result.value == 7


def test_images_and_tool_history(tmp_path):
    raw = b"image bytes"
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    transcript, images = _transcript(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "Look"},
                    {"type": "image_url", "image_url": {"url": url}},
                ]
            ),
            ToolMessage(content="Tapped", tool_call_id="tap-1"),
        ],
        tmp_path,
    )
    assert images[0].read_bytes() == raw
    records = json.loads(transcript)
    assert "Attached image 1" in records[0]["content"][1]
    assert records[1]["tool_call_id"] == "tap-1"


@pytest.mark.asyncio
async def test_unbound_tool_rejected(cli):
    cli["payload"] = {"content": "", "tool_calls": [{"name": "shell", "arguments": "{}"}]}
    with pytest.raises(ValueError, match="unbound"):
        await CodexChatModel().ainvoke("hello")


@pytest.mark.asyncio
async def test_cancellation_kills_process(monkeypatch):
    from artemis.llm import codex

    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -1)
    process.communicate.side_effect = [asyncio.CancelledError(), (b"", b"")]
    monkeypatch.setattr(codex, "find_codex", lambda: "codex.exe")
    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(asyncio.CancelledError):
        await CodexChatModel()._agenerate([HumanMessage(content="hello")])
    assert process.returncode == -1


@pytest.mark.asyncio
async def test_summary_provider_uses_configured_codex(monkeypatch):
    from artemis.services.llm import get_auxiliary_llm

    monkeypatch.setattr(
        "artemis.services.llm.get_default_llm_config",
        lambda: SimpleNamespace(
            operator=SimpleNamespace(
                provider="codex",
                reasoning_effort="xhigh",
                timeout=300,
            )
        ),
    )
    model = get_auxiliary_llm("gpt-6-luna")
    assert isinstance(model, CodexChatModel)
    assert model.reasoning_effort == "xhigh"


@pytest.mark.asyncio
async def test_cancel_during_spawn_waits_for_cli_exit_before_removing_inputs(monkeypatch):
    from artemis.llm import codex
    from pathlib import Path

    entered, release = asyncio.Event(), asyncio.Event()
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -1)
    process.communicate.return_value = (b"", b"")
    folders = []

    async def delayed_start(*args, **kwargs):
        folders.append(Path(kwargs["cwd"]))
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(codex, "find_codex", lambda: "codex.exe")
    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", delayed_start)
    task = asyncio.create_task(CodexChatModel().ainvoke("hello"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert folders[0].exists()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -1
    process.communicate.assert_awaited_once()
    assert not folders[0].exists()


def test_optional_judges_honor_explicit_provider_override():
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes

    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "codex",
                    "model": "gpt-6-luna",
                    "reasoning_effort": "xhigh",
                    "fallback": {"provider": "codex", "model": "gpt-6-luna"},
                },
                "nodes": {"validator_pixel_safety_net": {}, "planner_validation": {}},
            }
        )
    )
    assert config.get_agent("validator_pixel_safety_net").provider == "codex"
    assert config.get_agent("planner_validation").model == "gpt-6-luna"


def test_omitted_optional_judges_keep_native_defaults():
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes

    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "google",
                    "model": "gemini-3.8-flash",
                    "fallback": {"provider": "google", "model": "gemini-3.7-flash"},
                },
            }
        )
    )
    assert config.planner_validation is None
    assert config.get_agent("planner_validation").model == "gemini-3.5-flash-lite"
