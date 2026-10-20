"""LangChain adapter for the locally installed, signed-in Codex CLI.

Each call is ephemeral. Artemis owns tool execution; Codex only selects calls
in a JSON response. No OAuth tokens are read, copied, or stored by this module.
"""

import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import uuid

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def find_codex() -> str:
    explicit = os.environ.get("ARTEMIS_CODEX_PATH")
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise RuntimeError("ARTEMIS_CODEX_PATH does not point to a Codex executable")
    installed = shutil.which("codex")
    if installed:
        return installed
    # The desktop app is not always on PATH when start.bat is double-clicked.
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    root = local / "OpenAI" / "Codex" / "bin"
    candidates = [
        p
        for p in (
            local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe",
            root / "codex.exe",
        )
        if p.is_file()
    ]
    candidates.extend(root.glob("*/codex.exe") if root.is_dir() else [])
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise RuntimeError(
        "Codex CLI not found. Install Codex, run codex login, or set ARTEMIS_CODEX_PATH"
    )


async def _settle(task):
    """Keep ownership of CLI creation/exit even if the caller is cancelled again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _kill_and_reap(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


def _transcript(messages: list[BaseMessage], folder: Path) -> tuple[str, list[Path]]:
    records, images = [], []
    for message in messages:
        blocks = []
        content = message.content
        for block in [content] if isinstance(content, str) else content:
            if isinstance(block, str):
                blocks.append(block)
            elif block.get("type") in ("image_url", "image"):
                source = block.get("image_url", block.get("url", ""))
                if isinstance(source, dict):
                    source = source.get("url", "")
                if not source and block.get("base64"):
                    source = f"data:{block.get('mime_type', 'image/png')};base64,{block['base64']}"
                if not isinstance(source, str) or not source.startswith("data:image/"):
                    raise ValueError("Codex image inputs must use inline base64 image data")
                header, data = source.split(",", 1)
                if ";base64" not in header:
                    raise ValueError("Codex image input is not base64 encoded")
                ext = ".jpg" if "jpeg" in header else ".png"
                path = folder / f"image-{len(images)}{ext}"
                path.write_bytes(base64.b64decode(data, validate=True))
                images.append(path)
                blocks.append(f"[Attached image {len(images)}: {path.name}]")
            elif block.get("type") in ("text", "input_text"):
                blocks.append(block.get("text", ""))
            else:
                raise ValueError(f"Unsupported Codex content block: {block.get('type')}")
        record = {"role": message.type, "content": blocks}
        if getattr(message, "tool_calls", None):
            record["tool_calls"] = message.tool_calls
        if getattr(message, "tool_call_id", None):
            record["tool_call_id"] = message.tool_call_id
        records.append(record)
    return json.dumps(records, ensure_ascii=False), images


class CodexChatModel(BaseChatModel):
    """Use Codex authentication with text, screenshots, and Artemis tools.

    Responses are buffered until the CLI finishes; BaseChatModel provides its
    standard single-chunk streaming fallback. Async cancellation kills the CLI.
    """

    model_name: str = "gpt-6-luna"
    reasoning_effort: str = "xhigh"
    timeout_seconds: float = 300.0
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Any = None

    @property
    def _llm_type(self) -> str:
        return "artemis-codex"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name, "reasoning_effort": self.reasoning_effort}

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any):
        return self.model_copy(
            update={
                "tools": [convert_to_openai_tool(tool) for tool in tools],
                "tool_choice": tool_choice,
            }
        )

    def _schema(self) -> dict[str, Any]:
        names = [t["function"]["name"] for t in self.tools]
        choice = self.tool_choice
        if isinstance(choice, dict):
            choice = choice.get("function", {}).get("name", choice.get("name"))
        if choice in names:
            names = [choice]
        calls: dict[str, Any] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": names or ["__no_tools__"]},
                    "arguments": {
                        "type": "string",
                        "description": "Tool arguments encoded as a JSON object",
                    },
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        }
        if not names or choice == "none":
            calls["maxItems"] = 0
        elif choice in ("any", "required") or choice in names:
            calls["minItems"] = 1
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tool_calls": calls,
            },
            "required": ["content", "tool_calls"],
            "additionalProperties": False,
        }

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop:
            raise ValueError("Codex provider does not support stop sequences")
        with tempfile.TemporaryDirectory(prefix="artemis-codex-") as tmp:
            folder = Path(tmp)
            transcript, images = _transcript(messages, folder)
            schema_file, output_file = folder / "schema.json", folder / "response.json"
            schema_file.write_text(json.dumps(self._schema()), encoding="utf-8")
            prompt = (
                "You are the model backend for Artemis, a mobile automation agent. "
                "Follow the system instructions in the supplied conversation. "
                "Choose the next assistant response from the complete conversation. "
                "Artemis executes the declared tools; only return their names and JSON arguments. "
                "Do not execute tools, inspect the host, or change files yourself. "
                "Do not claim a tool succeeded before its result appears in the conversation.\n"
                f"Tool definitions: {json.dumps(self.tools, ensure_ascii=False)}\n"
                f"Tool choice: {json.dumps(self.tool_choice)}\nConversation:\n{transcript}"
            )
            command = [
                find_codex(),
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "--color",
                "never",
                "--model",
                self.model_name,
                "-c",
                f'model_reasoning_effort="{self.reasoning_effort}"',
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.apply_patch_freeform=false",
                "-c",
                'web_search="disabled"',
                "--output-schema",
                str(schema_file),
                "--output-last-message",
                str(output_file),
            ]
            for image in images:
                command.extend(["--image", str(image)])
            command.append("-")
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=folder,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # create_subprocess_exec can be cancelled after the OS process
                # exists but before it returns its handle. Recover that handle
                # before deleting temporary input files or releasing this call.
                process = await _settle(spawning)
                await _settle(asyncio.create_task(_kill_and_reap(process)))
                raise
            call_id = uuid.uuid4().hex
            pid = getattr(process, "pid", None)
            logger.info(f"CODEX_CALL start id={call_id} pid={pid} model={self.model_name} effort={self.reasoning_effort}")
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")),
                    timeout=self.timeout_seconds,
                )
            except (asyncio.CancelledError, TimeoutError):
                await _settle(asyncio.create_task(_kill_and_reap(process)))
                logger.info(f"CODEX_CALL reaped id={call_id} pid={pid} reason=cancel_or_timeout")
                raise
            logger.info(f"CODEX_CALL exit id={call_id} pid={pid} code={process.returncode}")
            if process.returncode:
                # Never include stdout/stderr: they may contain the conversation.
                raise RuntimeError(
                    f"Codex exited with status {process.returncode}. Check codex login status "
                    "and model access with the configured account."
                )
            if not output_file.is_file():
                raise RuntimeError("Codex completed without an assistant response")
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            allowed = {t["function"]["name"] for t in self.tools}
            calls = []
            for call in payload["tool_calls"]:
                if call["name"] not in allowed or self.tool_choice == "none":
                    raise ValueError("Codex returned an unbound tool")
                args = json.loads(call["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("Codex tool arguments must be a JSON object")
                calls.append({"name": call["name"], "args": args, "id": uuid.uuid4().hex})
            usage = None
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                event = json.loads(line)
                if event.get("type") == "turn.completed" and event.get("usage"):
                    raw = event["usage"]
                    inputs, outputs = raw.get("input_tokens", 0), raw.get("output_tokens", 0)
                    usage = {
                        "input_tokens": inputs,
                        "output_tokens": outputs,
                        "total_tokens": inputs + outputs,
                    }
            message = AIMessage(content=payload["content"], tool_calls=calls, usage_metadata=usage)
            return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return asyncio.run(self._agenerate(messages, stop=stop, **kwargs))
