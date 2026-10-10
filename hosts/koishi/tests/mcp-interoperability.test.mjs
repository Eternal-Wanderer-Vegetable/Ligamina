import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { LATEST_PROTOCOL_VERSION } from "@modelcontextprotocol/sdk/types.js";

const repositoryRoot = fileURLToPath(new URL("../../../", import.meta.url));

test("rmcp 3.5.1 and TypeScript SDK 1.32.1 negotiate 2025-11-25 over stdio", async () => {
  execFileSync("cargo", ["build", "--quiet", "--package", "ligamina-cli"], {
    cwd: repositoryRoot,
    stdio: "inherit",
  });

  const executable = path.join(
    repositoryRoot,
    "target",
    "debug",
    process.platform === "win32" ? "ligamina-cli.exe" : "ligamina-cli",
  );
  assert.ok(existsSync(executable), `missing server executable at ${executable}`);

  const transport = new StdioClientTransport({ command: executable, args: [], cwd: repositoryRoot });
  const client = new Client({ name: "ligamina-reference-acceptance", version: "0.1.0" }, { capabilities: {} });
  try {
    await client.connect(transport);
    // rmcp exposes only 2025-11-25 from this server, so a successful initialize
    // handshake proves the SDK's negotiated version is exactly that revision.
    assert.equal(LATEST_PROTOCOL_VERSION, "2025-11-25");
    assert.deepEqual(client.getServerVersion(), { name: "ligamina", version: "0.1.0" });

    const tools = await client.listTools();
    assert.deepEqual(tools.tools.map((tool) => tool.name), ["_ligamina_ping"]);
    const result = await client.callTool({ name: "_ligamina_ping", arguments: {} });
    assert.equal(result.content[0]?.type, "text");
    assert.equal(result.content[0]?.text, "pong");
  } finally {
    await client.close();
  }
});
