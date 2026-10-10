# ADR 0001: MVP toolchain and cross-language wire baseline

- Date: 2026-10-09; verification update: 2026-10-10
- Status: T01-T12 and G1 technical closure verified on Windows and local WSL Ubuntu 24.04.5.
- Scope: Rust Core daemon and capability Broker, Python NoneBot Host, TypeScript Koishi Host, v0.1 contracts, local stdio transport and process supervision, SQLite event/platform accounting, loopback management/gateway/MCP HTTP, reference clients, and differential harness.

## Decision

Use the exact toolchain and dependency versions below. Commit lock files and require locked installs. Do not change the machine's default Python or Node runtime.

| Layer | Locked baseline |
|---|---|
| Rust | rustc/cargo 1.97.1; `rust-toolchain.toml` selects `1.97.1-x86_64-pc-windows-msvc` |
| Python | CPython 3.12.14; `hosts/nonebot/pyproject.toml` constrains `==3.12.14.*`; install with `uv --locked` |
| Node | Node.js 24.20.0; Koishi package uses ESM and TypeScript 5.9.3 |
| NoneBot | nonebot2 2.5.0, nonebot-adapter-onebot 2.4.6; nonebug 0.4.4 is a development dependency |
| Koishi | `@koishijs/core` 4.18.11; `@koishijs/plugin-mock` 2.6.6 is a development dependency |
| MCP interop | rmcp 3.5.1 and `@modelcontextprotocol/sdk` 1.32.1; interop is pinned to `2025-11-25` |
| Local process control | `libc` 0.2.190 on Unix; `windows-sys` 0.61.2 for Windows Job Objects |
| SQLite persistence | `rusqlite` 0.40.2 with `bundled`; `sha2` 0.10.9 for credential digests and payload fingerprints |

SQLite is built from the pinned bundled SQLite source instead of depending on a machine's system SQLite. The registration credential and lease token are stored only as SHA-256 digests. Operation fingerprints include account, operation, target, and params while excluding `trace_id` and `operation_id`; the ledger is scoped by client/account/operation/operation ID. The Core creates a new epoch on open, revokes old gateway leases, and changes dispatched operations to `unknown`; pending operations remain claimable because they were not handed to a gateway.

`koishi` 4.18.11's meta-package failed during native startup on Node 24 with `@koishijs/loader: Class extends value #<Object> is not a constructor or null`. The plan explicitly allows `koishi / @koishijs/core` at the same candidate version, so the host now depends on `@koishijs/core` directly. Its native start/stop smoke passes. The Mock plugin remains test-only and does not stand in for a production Bridge Adapter.

The dependency audit found a vulnerable transitive `file-type` range through Koishi's HTTP plugin. Pin `file-type` 21.3.2 using the npm override; this is above the versions affected by the [ASF parser advisory](https://github.com/advisories/GHSA-5v7r-6r5c-r473) and the [ZIP decompression advisory](https://github.com/advisories/GHSA-j47w-4g3g-c36v). `npm audit` reports zero vulnerabilities with the override.

The independent MCP probe successfully negotiated `2025-11-25`, listed the Rust server's `ligamina` tool surface, and called the reserved `_ligamina_ping` health tool. Its leading underscore cannot occur in a valid capability ID, preventing a tool-name collision. rmcp 3.5.1 advertises only this protocol version in the pinned server configuration; this decision does not claim compatibility with a 2026 MCP revision.

## Runtime provenance and fingerprints

The hashes below identify the local executables used for development checks. They are not hashes of redistributable installers or container image archives. T15 must pin and verify the exact deployment artifacts separately.

| Runtime | Local file | SHA-256 | License / source record |
|---|---|---|---|
| CPython 3.12.14 | uv-managed `python.exe` | `4EB51B7D5963D9E0DC356BD209B1D55360C73DB39D8D458CEEE084610CA48FD1` | PSF license; adjacent `LICENSE.txt` SHA-256 `886A0EAD2D89030EE62DBFF52B04E47AB91998341295BB9C56FB952B4E081C7A`; Python 3.12.14 [release page](https://www.python.org/downloads/release/python-31214/) |
| Node.js 24.20.0 | `C:\Program Files\nodejs\node.exe` | `5C976096E04E5C2C1F091938926234CC9FBEBFE9787DDD149351B3B0ECC707B5` | MIT; official [v24.20.0 release artifacts and checksums](https://nodejs.org/download/release/v24.20.0/) |
| Rust 1.97.1 | rustup toolchain `bin/rustc.exe` | `CF79CFD77B0A144C56A0A6AF6BF10BCDF095A73718CD4BF2B9D4FE2D2CBDED55` | MIT OR Apache-2.0; official [1.97.1 release announcement](https://blog.rust-lang.org/2026/07/16/Rust-1.97.1/) |
| CPython 3.12.14 (Linux) | uv-managed `bin/python3.12` | `8F9781A98200D9ECDA7E00464E4C64B1327ABAE788AE8E6979D5C859311410C7` | PSF license; Python 3.12.14 release page above |
| Node.js 24.20.0 (Linux) | `/opt/node-v24.20.0/bin/node` | `89AF8424DD53E560B1933F87BA650D8BF57C83CA5A04600EEFB31F416AABBAE7` | MIT; official v24.20.0 release artifacts above |
| Rust 1.97.1 (Linux) | rustup toolchain `bin/rustc` | `D3A664C970A9FD8361B64194861BEBC1AE37B9054E5EE3400DC1C9E691797EEA` | MIT OR Apache-2.0; official 1.97.1 release announcement above |

The Windows machine's default Python remains 3.14.7 and default Node remains 24.20.0. Full Linux checks now run in a local WSL Ubuntu 24.04.5 LTS distribution with the exact runtime versions above. Earlier Linux container checks used official versioned images `python:3.12.14-slim`, `node:24.20.0-bookworm-slim`, and `rust:1.97.1-bookworm`; image digests remain a deployment-manifest item for T15/T16.

## Wire and transport decisions

- The language-neutral JSON Schemas under `contracts/v0.1/` are authoritative. Rust, Python, and TypeScript all consume the same positive/negative fixtures in `contracts/v0.1/fixtures/vectors.json`.
- Platform IDs remain strings; schema and vectors include values beyond signed 64-bit range and distinguish group from private conversation identity.
- A reused `operation_id` is idempotent when `account_id`, `operation`, `target`, and `params` match. `trace_id` is observability metadata and may differ on a retry. A changed side-effect payload returns `operation_conflict`.
- Core/Host traffic uses one JSON object per NDJSON line over stdio. The Core accepts and emits frames up to 1 MiB, correlates concurrent requests by string ID, allows 64 outstanding Core requests per Host, and fails pending calls on EOF or terminal protocol errors. Stderr is drained separately from protocol stdout.
- Deadline and cancellation remove the pending correlation entry and send `request.cancel`; any later response is counted and ignored.
- `host.initialize` returns an explicit pure-capability snapshot. Core binds that snapshot to the trusted Host ID/epoch; `capability.invoke` validates both arguments and results against the declared schemas. A Host can request another capability with `service.invoke`; the Core owns the caller identity and call chain.

## Host supervision and local process trees

- `HostSupervisor::apply_desired_set`, `status`, and `shutdown` own the local Host set. Disabled Hosts do not spawn a process. A Host becomes active only after `host.initialize` returns both `ready: true` and protocol version `0.1`.
- Every launch has a monotonically increasing epoch. Host handles and Host-originated Core requests carry that epoch; inactive or stale generations are rejected. Process exit and shutdown cancel the generation token so in-flight Core handlers can stop.
- Each Host has an independent process/session monitor. Restarts use a per-Host maximum count and fixed backoff; exhausting the budget leaves that Host failed without stopping its siblings.
- Unix launches use a new process group and clean the full group with TERM/KILL. Windows launches start suspended, attach to a Job Object configured with kill-on-close, then resume the primary thread. This closes the spawn-to-job race and lets descendants inherit job membership.
- The supervisor integration worker exercises readiness rejection, desired-set disablement, restart exhaustion, stale epochs, sibling isolation, reverse requests, and descendant cleanup.
- `CapabilityBroker` atomically replaces descriptors by Host generation, rejects epoch rollback and inactive callers, and bounds invocation calls to eight frames. `CoreRuntime` binds generation changes from `HostSupervisor` to Broker snapshots and revokes descriptors when a Host exits. A production smoke verifies revocation while its sibling Host keeps serving.

## Event receipts, routing, and reference client

- SQLite schema version 2 adds event receipts scoped by client, platform, account, conversation kind/ID, and event ID. A matching retry ignores `trace_id`; a changed semantic payload is rejected as `event_conflict`.
- `EventRouter` accepts only configured Host IDs, persists the event before returning `accepted`, serializes work per conversation, and bounds per-conversation backlog, global outstanding events, active conversations, and tracked queue workers. A duplicate is never queued again. Core reopen changes accepted/processing receipts to `unknown` and does not replay them.
- The reference TypeScript client covers management, event, operation-status, and customer-gateway request modes. Its CLI reads credentials from environment variables, not command-line arguments. Its virtual platform models account/conversation-scoped sends and message history for tests.
- The module-level router tests still use a processor callback and the reference-client unit tests still use injected `fetch`. T12 adds separate black-box evidence against the live Core daemon, real supervised Host processes, and the production HTTP routes; both Windows and WSL reports are saved under `outputs/acceptance/`.

## Production daemon and G1 federation

- `CoreRuntime` opens the configured SQLite store, installs client/Host grants and gateway registrations, starts the desired local Hosts, binds their active capability snapshots, and routes `service.invoke`, `platform.execute`, and durable event dispatch through the current Host epoch.
- `ligamina-cli daemon --config <path>` serves only on a loopback socket. The HTTP surface includes status, event submit/status, capability discovery/invocation, operation status, gateway lease/claim/result, graceful shutdown, and Streamable HTTP MCP at `/mcp`. Management and Client APIs use separate bearer credentials; the MCP service enforces its configured Host and Origin allowlists.
- `ligamina-cli status --url http://127.0.0.1:<port>` and `doctor` read the admin token from `LIGAMINA_ADMIN_TOKEN` and reject non-loopback destinations. `report <json>` prints the acceptance summary. The lab stability runner measures Host capability revocation/restoration, recovery probes, RSS windows, and process cleanup.
- `lab/run.py --suite smoke` stores the complete redacted request/event/operation/receipt trace and an environment/lockfile fingerprint. It starts both real locked Hosts with self-authored native plugins, routes group and private events, completes four virtual gateway operations, calls each pure capability by HTTP and by the independent TypeScript MCP SDK, kills one Host, checks capability revocation and sibling availability, verifies an injected wrong-recipient failure, then checks process cleanup.
- G1 passed on Windows 11 and WSL Ubuntu 24.04.5 on 2026-10-10. The platform gateway is deliberately virtual, and the plugins are self-authored fixtures; this proves the planned federation path, not live QQ/platform network behavior or third-party plugin compatibility. G2 remains `NOT READY`.

## Differential harness scaffolding

- `lab/native_runner.py` starts native and federated harness commands as separate processes, sends only the case stimulus on stdin, keeps stderr out of protocol stdout, records environment-missing/timeout/failed states, and writes a JSON report.
- `lab/compare.py` checks each result against deterministic expected events, operations, receipts, and cleanup before comparing the two outputs. Thus identical wrong results fail. The fixture fingerprints use the locked NoneBot CPython/uv/profile inputs.
- Self-authored NoneBot and Koishi fixtures now exercise the real native Bridge Adapters and Worker entrypoints. The T10 cross-Host test starts both locked workers: a NoneBot Matcher sends `service.invoke`, the Rust Broker routes `capability.invoke` to Koishi, and the result returns through the platform gateway. Third-party plugin differential coverage and the full lab comparison report remain pending T13.

## Locked dependency file fingerprints

These hashes make this decision record auditable against the lock files tested on 2026-10-10.

| File | SHA-256 |
|---|---|
| `Cargo.lock` | `617242B5952384FDC6CA1413F7101987034D566C338AA51C9BCA708C29A8FF6` |
| `clients/reference/package-lock.json` | `2A6BFD208047B9F18E05C19AB498AABEC5F275D44D7B71A3FE9E3D76F6E73F70` |
| `hosts/nonebot/uv.lock` | `D63536AE791DD2BD9E254F3E5D5951D5E8F14BD599056548D55E53126497727A` |
| `hosts/koishi/package-lock.json` | `FAEDF756729A0764F14C633063225F57507A6B191F40D4E83F83A3097C4A1946` |

## Verification record

| Command / experiment | Result |
|---|---|
| Windows Rust workspace | `cargo test --workspace --locked --offline` passed 26 tests: 1 MCP protocol integration, 3 shared contract tests, 1 SQLite restart test, 5 Broker tests, 2 event-router tests, 6 HostSession tests (including live NoneBot→Broker→Koishi), 3 platform-store tests, and 5 supervisor lifecycle tests; Windows Job Object descendant cleanup passed |
| Windows reference client | `clients/reference: npm install --save-exact @modelcontextprotocol/sdk@1.32.1` reported 0 vulnerabilities; `npm test` passed 5 tests, including the independent MCP stdio client |
| Windows differential tooling | `uv run --locked --project hosts/nonebot --python 3.12.14 python -m unittest discover -s lab/tests -v` passed 9 tests, including positive dual-process fixture execution and same-error/wrong-recipient/duplicate/segment-loss/cleanup injections |
| Windows T12 production smoke | `uv run --project lab python lab/run.py --suite smoke --platform windows` passed: 4 routed events, 4 gateway receipts, HTTP and independent MCP calls to both Hosts, crash/revoke/sibling continuity, wrong-recipient injection detected, and zero remaining Host PIDs; see [`t12-smoke-windows-20261010-083241.json`](../../outputs/acceptance/t12-smoke-windows-20261010-083241.json) |
| Windows Python Host | `uv run --locked --project hosts/nonebot --python 3.12.14 pytest -q hosts/nonebot/tests` (3 passed), plus `native_smoke.py`, `native_bridge.py`, and `worker_smoke.py` passed |
| Windows Koishi Host | `npm test` passed 6 contract/native/worker tests; `npm run test:mcp` passed |
| Windows MCP protocol | Rust MCP integration fixture passed legacy 2025-11-25 initialize/list/call, schema and tool errors, cancellation propagation, dynamic list-changed notification, and rejection of impure/message-send capabilities |
| Linux toolchain | WSL Ubuntu 24.04.5 LTS: rustc/cargo 1.97.1, CPython 3.12.14, uv 0.12.5, Node 24.20.0, npm 11.19.0 |
| Linux Python Host (WSL) | `uv run --locked --project hosts/nonebot --python 3.12.14 pytest -q hosts/nonebot/tests` (3 passed), plus native lifecycle, Bridge Adapter and Worker smoke checks passed |
| Linux Koishi Host (WSL) | `npm test` passed 6 contract/native/worker tests |
| Linux MCP interop (WSL) | Koishi `npm run test:mcp` passed (1 test); rmcp 3.5.1 and TypeScript SDK 1.32.1 negotiated 2025-11-25 over stdio |
| Linux Rust workspace (WSL) | `CARGO_NET_OFFLINE=true cargo test --workspace --locked` passed 26 tests, including MCP cancellation and tool-list notification; 6 HostSession tests include live NoneBot→Broker→Koishi. Unix process-group descendant cleanup passed. The WSL command includes `/opt/node-v24.20.0/bin` in `PATH`. |
| Linux reference client (WSL) | `npm ci` installed the locked SDK/types/compiler and reported 0 vulnerabilities; `npm test` passed 5 tests, including MCP stdio initialize and ping |
| Linux differential tooling (WSL) | The locked CPython 3.12.14 unittest command passed 9 tests |
| Linux T12 production smoke (WSL) | `CARGO_NET_OFFLINE=true uv run --project lab python lab/run.py --suite smoke --platform linux` passed the same G1 path and cleanup checks; see [`t12-smoke-linux-20261010.json`](../../outputs/acceptance/t12-smoke-linux-20261010.json) |

The WSL test copy at `/root/ligamina` was checked against the mounted Windows workspace: SHA-256 hashes matched for the lock files, Rust supervisor/transport code and tests, Python worker scripts, and native smoke tests. Linux `.venv`, `node_modules`, and Cargo `target` outputs stayed in WSL and did not alter the Windows checkout. A first container-based Python attempt created a Linux `.venv` symlink in the shared mount; the generated environment was moved aside, the standard Windows `.venv` was rebuilt, and the exact Windows checks then passed. On the final WSL rerun, a separate duplicate `npm audit --audit-level=moderate` command produced no output and was interrupted; `npm ci` itself completed its audit and reported zero vulnerabilities.

## Consequences and remaining scope

T01–T12 now establish locked baselines, cross-language contracts, bidirectional transport, local Host supervision, SQLite-backed event/platform lifecycles, the reference clients, both native Bridge Adapters, the generation-scoped capability Broker, production Core HTTP/MCP wiring, and G1 federation. Module-level tests remain focused on isolated interfaces; the T12 acceptance reports separately prove their live composition through the daemon and supervised Host processes. Third-party plugin compatibility, long-duration stability, deployment, CI, and clean-system G2 acceptance remain future tasks. The Windows `.venv` and `node_modules`, WSL dependency trees, and build outputs are generated local artifacts.

## 2026-10-10 T14/T15 execution notes

T14 adds loopback-only CLI `status`/`doctor` checks, JSON `report` summaries, production-Core crash/recovery and RSS monitoring. Managed Host shutdown is concurrent. The NoneBot reader and Koishi readline loop now process `host.shutdown` inline and exit after acknowledging it. Windows and WSL quick production probes each recovered both Hosts and shut down cleanly; the quick resource window is intentionally `skipped`, so the T14 gate remains open until the full WSL 100-cycle/24-hour report completes.

The initial Windows x64 runtime bundle uses uv 0.12.5 to install a managed CPython 3.12.14 into a versioned application directory. Python.org publishes 3.12.14 as a source-only security release without a Windows installer, so the bundle does not claim that release ships a native installer. The uv archive and Node 24.20.0 Windows zip have pinned SHA-256 values and their license notices are retained; the Python license is copied from the managed distribution. The Python install uses `--no-registry --no-bin`, and Node stays under the application directory. Candidate installation and locked Host dependencies are validated before `active.json` switches versions. A local Windows probe under a Chinese, spaced path passed both Host environment installation, and an injected Node hash failure left the prior active version unchanged. First-run daemon configuration, fresh-VM execution, and C14 remain open.
