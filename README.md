# Ligamina

**English** | [简体中文](README.zh-CN.md)

Ligamina runs chat-bot plugins from **Koishi**, **NoneBot** and **AstrBot** unmodified, inside their native frameworks, and makes them usable from **any bot or AI coding agent** through one MCP-style interface. A Rust Core supervises one isolated Host process per framework; plugins keep their own command parsing, middleware, permissions and storage.

It is also a compatibility lab: official plugin catalogs are frozen with our own hashes, plugins are tested in OS-isolated Workers, and every result is bound to an exact plugin version, artifact hash, dependency lock and test case.

## How it works

```text
 Claude Code / other MCP clients        Bots that want group messages to reach plugins
 LLM-based bots (e.g. Stella)           (any OneBot v11 implementation, other platforms)
            |  MCP tools                              |  events + Gateway (HTTP)
            v                                         v
   +---------------------------- Rust Core ----------------------------+
   |  command catalog · tool sessions · event router · operation ledger |
   +------+----------------------+----------------------+-------------+
          | NDJSON stdio         |                      |
   Koishi Host            NoneBot Host           AstrBot Host
   (@koishijs/core)       (nonebot2 2.5.0)       (astrbot 4.29.1)
          |                      |                      |
   native plugins         native plugins         native plugins
```

- **Plugins as tools.** Each Host reports the commands its plugins registered. Core lists them as MCP tools named `cmd.<host>.<command>`. A tool call runs in a fresh virtual private conversation through the framework's normal dispatch, and the plugin's replies come back as the tool result, with no Gateway or platform account involved.
- **Plugins for bots.** A bot that owns a platform connection submits events and runs a Gateway that performs the plugins' outgoing messages. The reference [OneBot v11 Gateway](clients/reference/src/onebot-gateway.ts) works with any OneBot implementation. Ligamina contains no bot-specific code ([ADR 0002](docs/adr/0002-plugins-as-tools.md)).

## Status

| Area | State |
| --- | --- |
| Core, Koishi and NoneBot Hosts, HTTP and MCP | Working; Phase 1 acceptance passed (4 exact plugin versions behavior-verified) |
| MCP command tools (Koishi, NoneBot, AstrBot) | Working; frozen catalog plugins passed 18/18 tool-level runs in network-disabled Linux Workers |
| AstrBot Host | Working on the unmodified framework; event-path parity tested; market catalog frozen (3,265 plugins); install/load pilot 21/24 loaded in network-disabled Linux Workers; behavior acceptance next ([plan](docs/plans/2026-11-07-phase-5-astrbot-host.md)) |
| Catalog-wide compatibility | Not claimed. Census and recovery ledgers are closed, but behavior and automation gates remain blocked (details in [简体中文](README.zh-CN.md)) |
| Real-platform delivery | Blocked: the tool path works up to the bot, but the first test bot's own delivery check drops tool replies (reported to that bot) |
| Windows Sandbox Enforcement | Blocked on a signing identity accepted by the current App Control policy |

Current details: [progress](progress.md), [feature list](feature_list.json), [handoff](session-handoff.md).

## Quick start

Build the Core and the Hosts you need:

```powershell
cargo build -p ligamina-cli --locked
cd hosts/koishi; npm ci; npm run build; cd ../..
uv sync --locked --project hosts/nonebot --python 3.12.14
uv sync --locked --project hosts/astrbot --python 3.12.14
```

Start the daemon with a config file (`target/debug/ligamina-cli daemon --config daemon.json`). A minimal config exposing Koishi plugin commands as tools:

```json
{
  "listen": "127.0.0.1:8765",
  "database_path": "data/core.sqlite3",
  "admin_token": "<at least 24 random bytes>",
  "mcp_client_id": "agent",
  "clients": [{ "id": "agent", "token": "<at least 24 random bytes>" }],
  "gateways": [{ "id": "unused", "client_id": "agent", "account_id": "10001", "registration_token": "<at least 32 random bytes>" }],
  "hosts": [{ "id": "koishi", "program": "node", "args": ["dist/worker.js"], "current_dir": "hosts/koishi",
               "environment": { "LIGAMINA_KOISHI_PLUGINS": "<plugin import specifiers>" } }],
  "command_tools": { "enabled": true, "allow": ["cmd.koishi.*"] }
}
```

Command tools are off unless `command_tools.enabled` is set. The daemon currently requires one Gateway entry even when only MCP is used. Connect an MCP client, for example Claude Code:

```powershell
claude mcp add --transport http plugins http://127.0.0.1:8765/mcp --header "Authorization: Bearer <client token>"
```

Tool input is raw text (`{"arguments": "..."}`) or, for Koishi and AstrBot commands, typed fields generated from the command's declaration. Bot integration through events and a Gateway is described in [Bot and agent integration](docs/guides/bot-integration.md).

## Hosts

| Host | Framework | Notes |
| --- | --- | --- |
| [Koishi](hosts/koishi/README.md) | `@koishijs/core` 4.18.11 (Node 24.20.0) | Native Session dispatch; typed arguments and options as tool fields |
| [NoneBot](hosts/nonebot/README.md) | `nonebot2` 2.5.0, OneBot v11 adapter (Python 3.12.14) | `on_command` / `on_shell_command` matchers; command prefix from the bot's config |
| [AstrBot](hosts/astrbot/README.md) | `astrbot` 4.29.1, AGPL-3.0, unmodified (Python 3.12.14) | Headless real lifecycle with a bridge platform adapter; built-in admin commands never exposed |

## Safety model

- Tool sessions use reserved accounts that no grant or Gateway may use; their replies are completed inside Core and never reach the operation ledger or a platform.
- Uncertain deliveries stay `unknown` and are never replayed automatically.
- Third-party catalog plugins run only in admitted OS-isolated Workers (network-disabled Linux Docker, Windows Sandbox); plugin archives are pinned by our own hashes.
- Logs and reports never contain tokens, and stdout of every Host is reserved for the NDJSON protocol.

## Verify

```powershell
pwsh -NoProfile -File init.ps1 -Mode docs   # documentation, evidence and tracker checks
pwsh -NoProfile -File init.ps1 -Mode full   # locked builds, all test suites, smoke tests
```

On Linux/WSL use `bash init.sh docs` or `bash init.sh full`. See [development and verification](docs/guides/development.md).

## Documentation

- [Documentation map](docs/README.md): architecture, development, compatibility, reporting, integration.
- [Evidence index](docs/evidence/README.md): acceptance reports and historical decisions.
- Plans: [Phase 4 behavior readiness](docs/plans/2026-10-26-phase-4-behavior-readiness.md), [plugin tool surface](docs/plans/2026-11-07-plugin-tool-surface.md), [Phase 5 AstrBot Host](docs/plans/2026-11-07-phase-5-astrbot-host.md).
- Agent entry point: [AGENTS.md](AGENTS.md).

## License

See [LICENSE](LICENSE) (GNU AGPL-3.0). The AstrBot Host depends on AstrBot (AGPL-3.0-or-later) as an unmodified, separately run dependency.
