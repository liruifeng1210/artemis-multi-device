# Use the signed-in Codex account

This branch restores the local Codex/Luna adaptation from backup commit
`43d0fd7`. Earlier generic multi-device commits excluded these files, which is
why switching to a clean branch lost the working setup. Keep the adapter and
its workspace configuration together when moving this setup to another branch.

The workspace default is `provider: codex`, model `gpt-6-luna`, reasoning effort
`xhigh`. This uses the locally installed Codex CLI and its existing account;
no `OPENAI_API_KEY` is required. Usage counts against that account's available
Codex allowance. The adapter never reads or copies OAuth tokens.

1. Install Codex or the Codex desktop app, and sign in (`codex login`).
2. Run `start.bat` and open <http://localhost:8000/workspace>.
3. If Codex is not found, set `ARTEMIS_CODEX_PATH` in `.env` to its executable.

The adapter starts an ephemeral, read-only `codex exec` invocation per model
request, with the user config ignored and shell, patch, and web tools disabled.
Screenshots are passed as temporary local images. Conversation history and
Artemis tool definitions are sent as input; structured JSON output is translated
back to LangChain tool calls. Artemis continues to execute all device tools.
Temporary images and response files are deleted when each request finishes.

This adds CLI startup and prompt overhead to each request. Responses are buffered
until a complete model turn is available, rather than streamed token by token.
Model access and account limits are determined by Codex; installing this adapter
does not provide unlimited or free inference. Gemini ER-specific grounding is
replaced by the universal Pro explorer in this workspace's configuration.

This is a model backend adapter using the signed-in account, not a connection
to the current desktop chat's conversation. Artemis retains each Role's native
transcript and sends the relevant history on each request. Each concurrent
request has its own temporary files and CLI process. Cancellation kills and
reaps that request's process, including cancellation during process creation.

The model name passed to the installed CLI is `gpt-6-luna`; `xhigh` is the
separate reasoning setting. The combined UI label is not used as an API model ID.

The server explicitly returns JavaScript MIME types to avoid Windows registry
entries such as `.js = text/plain` causing a blank Angular page.
After restarting an already-running server, force-refresh an old browser tab
to discard cached responses with the incorrect MIME type.

## Two-phone persistent Session acceptance

```powershell
.venv/Scripts/python.exe -m examples.calculator_roles --device-a SERIAL_A --device-b SERIAL_B --cancel-test
```

This runs the real Pro graph with one persistent Role per phone. Round one is
A: `125+75` and B: `48/6`. Round two continues the current displays with A: `*3`
and B: `+17`. The optional cancellation round cancels A's exact task attempt
while B continues, then submits another assignment to A without reopening its
Session. Screenshots, hierarchy dumps, task results and Session IDs are saved
in the printed output directory. Video recording is disabled for this example;
native Planner, Operator, Validator, Checker and memory remain enabled.

The example uses the RoleSession SDK. The native web UI can display Session
telemetry, but its PID-based stop controls are not the independent Role cancel
API; use `role.cancel(task_id, attempt_id)` for this test.
