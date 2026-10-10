# Reference client

This package provides the first typed caller for the local management, event, and customer-gateway HTTP contract, plus a small in-memory platform for isolated send/history exercises. It does not contain or emulate a Ligamina HTTP server.

It also includes an independent MCP SDK client in `src/mcp-client.ts`. The wrapper accepts an SDK transport, completes the SDK initialization lifecycle, and exposes typed tool discovery and invocation. The locked `@modelcontextprotocol/sdk` version is 1.32.1. Its interoperability test launches the local `ligamina-cli` stdio endpoint and calls `_ligamina_ping`, a reserved internal tool name that cannot collide with a valid capability ID. Dynamic Host capability routing is covered by the Rust Broker protocol integration test and will be connected to the production Core lifecycle in T12.

## Client modes

Build with `npm ci && npm test`. The CLI reads `LIGAMINA_URL` (default `http://127.0.0.1:8765`) and `LIGAMINA_TOKEN` from the environment. It reads request JSON from a file and prints the JSON response:

```text
node dist/cli.js status
node dist/cli.js host-set <host-id> <desired.json>
node dist/cli.js event <event.json>
node dist/cli.js event-status <event-id>
node dist/cli.js operation-status <operation-id>
```

Gateway registration uses `LIGAMINA_GATEWAY_REGISTRATION_TOKEN`; action claim and result calls use the short-lived `LIGAMINA_GATEWAY_LEASE_TOKEN`. Tokens are never accepted as command-line arguments. The Core HTTP endpoints are a later integration item; until they exist, client requests can be exercised with a test server or injected `fetch` implementation.

`VirtualPlatform` is an in-memory test double for a customer-owned gateway. It keeps history isolated by account and conversation kind/ID, preserves structured message segments, and returns the prior receipt for a repeated matching operation ID. It does not make real platform connections or replace the Core's durable ledger.
