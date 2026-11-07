# AstrBot Host

Runs AstrBot plugins inside the **unmodified AstrBot framework** (`astrbot==4.29.1`, AGPL-3.0-or-later, locked in `uv.lock`), like the NoneBot and Koishi Hosts run their native frameworks. Ligamina code never copies or patches AstrBot; it only adds a platform adapter and a stdio worker in a separate process.

## How it works

- `ligamina_astrbot/lifecycle.py` sets `ASTRBOT_ROOT` and the working directory to the Host's data directory **before** importing AstrBot (AstrBot creates `data/`, `cmd_config.json` and SQLite databases at import time), forces a headless config (no LLM provider, no remote text-to-image, telemetry off, only the bridge platform), and runs the real `AstrBotCoreLifecycle`. The dashboard is never started.
- `ligamina_astrbot/bridge.py` registers the `ligamina_bridge` platform adapter. Core event envelopes become native `AstrBotMessage` events committed to AstrBot's event queue, so the full pipeline runs (waking, whitelist, rate limit, command dispatch, result decoration, respond). Sends become Core `platform.execute` operations. The scheduler's end-of-event cleanup call is the completion signal for `event.dispatch`.
- `ligamina_astrbot/commands.py` reports `filter.command` handlers (aliases, groups as `group.sub`, typed parameters, `GreedyStr` as a rest-of-line `text` argument) as v0.2 command descriptors for MCP command tools. AstrBot's built-in stars (`builtin_commands`, `astrbot`) are **never** reported: they include plugin installation and configuration commands.
- `ligamina_astrbot/worker.py` is the NDJSON JSON-RPC stdio worker. It keeps the real stdout for protocol frames and points `sys.stdout` at stderr, because AstrBot logs to stdout.

## Configuration

- `LIGAMINA_ASTRBOT_PLUGINS`: plugin directories (each with `main.py` and `metadata.yaml`), separated by the OS path separator. They are copied into `<data>/data/plugins/`.
- `LIGAMINA_ASTRBOT_DATA_DIR`: the Host's AstrBot root (default `./astrbot-data`).

## Run and verify

```powershell
uv sync --locked --python 3.12.14
uv run --locked --python 3.12.14 python tests/bridge_smoke.py
uv run --locked --python 3.12.14 python -m ligamina_astrbot.worker   # started by Core in practice
```

`tests/bridge_smoke.py` boots the real lifecycle headless with the self-authored `tests/fixtures/ligamina_smoke` plugin and checks command discovery plus private and group dispatch. The Rust test `astrbot_commands_are_callable_as_mcp_tools_through_the_real_framework` (`crates/ligamina-cli/tests/command_tools.rs`) runs this Host under the production Core and calls its commands over MCP.

## Known limits

- Plugins that restrict handlers with `filter.platform_adapter_type(...)` to a real platform (for example `aiocqhttp`) do not match the bridge platform, and code that reaches for a platform's raw client (such as `AiocqhttpMessageEvent.bot`) is unavailable.
- Proactive `send_by_session` is refused (`unsupported_api`) until a ledger-backed path is designed.
- Only text, mention, reply and image message components are mapped; others fail with `unsupported_api`.
- AstrBot installs missing plugin requirements lazily with `pip` at import time. Catalog plugins must therefore run in admitted OS-isolated Workers with frozen, pre-installed dependencies, never on a developer machine.
