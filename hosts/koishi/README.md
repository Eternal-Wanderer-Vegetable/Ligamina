# Koishi Host

This profile pins `@koishijs/core` 4.18.11, Satori APIs supplied by that lock, and `@minatojs/driver-sqlite` 4.7.0. Events create native Satori `Session` instances through `Bot.session()` and enter `Bot.dispatch()`; command parsing, middleware order, permissions, and plugin dependencies stay within the locked Koishi context.

Run the worker from this project after `npm ci` and `npm run build`:

```powershell
node dist/worker.js
```

Configuration uses environment variables:

- `LIGAMINA_KOISHI_PLUGINS`: comma-separated import specifiers for native Koishi plugins. Absolute file paths should use `file:` URLs.
- `LIGAMINA_KOISHI_PLUGIN_CONFIG`: JSON object mapping plugin specifiers to their configuration objects.
- `LIGAMINA_KOISHI_NICKNAMES`: comma-separated native Koishi nicknames.
- `LIGAMINA_KOISHI_DATA_DIR`: persistent directory for `koishi.sqlite`; defaults to `./data` under the worker's working directory.

The Bridge Bot maps group conversations to Satori text channels plus guild IDs, and private conversations to direct channels. It resolves private send targets from `Session.userId`, so identical string IDs in group and private conversations remain distinct. Incoming reply segments are fetched through `get_message` and attached as the native `Session.quote`; sent messages and reads use `send_message`/`get_message` operations. The adapter advertises only message create/read and login read platform operations. Other Satori methods and `bot.internal` calls fail with `unsupported_api`.

The worker also reports its pure `ligamina.koishi.math.add` capability at initialization and handles `capability.invoke` after schema validation. Native plugins can call the Core through `session.bot.adapter.invokeService(...)`; the cross-Host fixture verifies the corresponding NoneBot-to-Koishi route through the Rust Broker.

Framework/plugin stdout writes are redirected to stderr after the worker captures its protocol writer. The local SQLite driver gives the profile a real persistent database without a native binary dependency. A restart smoke test closes and reopens the same data directory, then reads the saved Koishi user authority back from SQLite.

Verification from this project:

```powershell
npm ci
npm test
npm run test:mcp
```

The native plugin used by the Host smoke tests is self-authored and demonstrates Host mechanics. Separate lab smoke tests now exercise the production Core HTTP routes, supervised Hosts, cross-Host capabilities, and virtual gateway receipts. The [T18 audit](../../outputs/acceptance/t18-final-audit-20261012.json) closes first-stage acceptance and limits third-party behavior support to exact tested versions. Full official-catalog coverage, independent native baselines, and broader service/loader profiles are [phase-two work](../../docs/plans/2026-10-12-phase-2-full-catalog.md).
