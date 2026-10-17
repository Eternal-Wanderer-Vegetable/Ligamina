# NoneBot Host

This locked profile runs NoneBot 2.5.0 and OneBot v11 models from `nonebot-adapter-onebot` 2.4.6. It receives Ligamina event envelopes over the Host stdio protocol and passes them through OneBot `Adapter.json_to_event` and `Bot.handle_event`; it does not open a OneBot network connection.

The worker is started from this project with:

```powershell
uv run --locked --python 3.12.14 python -m ligamina_nonebot.worker
```

`LIGAMINA_NONEBOT_PLUGINS` is a comma-separated list of importable NoneBot plugin modules. `LIGAMINA_NONEBOT_NICKNAMES` is a comma-separated list of names used by OneBot's native nickname preprocessor. Framework logs go to stderr; stdout is reserved for JSON-RPC/NDJSON.

The Bridge Adapter supports `send_msg`, `send_group_msg`, `send_private_msg`, `get_msg`, and `get_login_info`. The three message-send APIs map to the customer gateway operation `send_message`; message lookup and account information map to `get_message` and `get_self_info`. Other OneBot APIs and unrecognized parameters fail with `unsupported_api`. `get_msg` requires a known conversation scope from the current event or a message previously sent by the same adapter.

The pinned driver lifecycle uses NoneBot 2.5.0's private `_lifespan` context manager and `_adapters` registry in `ligamina_nonebot/lifecycle.py`; startup/shutdown smoke checks cover this seam. The worker implements `host.initialize`, `event.dispatch`, `capability.invoke`, `runtime.health`, `host.shutdown`/`runtime.stop`, and reverse `platform.execute` and `service.invoke` requests. Its fixture declares the pure capability `ligamina.nonebot.math.add`; plugins can call a named capability through `bot.adapter.invoke_service(...)`. The Rust Core daemon binds these routes and supervises the worker; production integration is covered by the first-stage smoke and [T18 audit](../../outputs/acceptance/t18-final-audit-20261012.json).

Verification commands:

```powershell
uv run --locked --python 3.12.14 pytest -q
uv run --locked --python 3.12.14 python tests/native_smoke.py
uv run --locked --python 3.12.14 python tests/native_bridge.py
uv run --locked --python 3.12.14 python tests/worker_smoke.py
```

The commands above run from this Host directory; from the repository root, use the exact paths in [scripts/check.ps1](../../scripts/check.ps1) or [check.sh](../../scripts/check.sh). Third-party support remains limited to the audited exact versions and fixtures, with virtual gateway receipts. Full official-catalog testing is [planned for phase two](../../docs/plans/2026-10-12-phase-2-full-catalog.md).
