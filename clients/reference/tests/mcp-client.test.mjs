import assert from "node:assert/strict";
import { test } from "node:test";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { LigaminaMcpClient } from "../dist/mcp-client.js";

test("independent TypeScript MCP SDK completes the legacy stdio lifecycle", async () => {
  const transport = new StdioClientTransport({
    command: process.env.CARGO ?? "cargo",
    args: [
      "run",
      "--quiet",
      "--offline",
      "--manifest-path",
      "../../Cargo.toml",
      "-p",
      "ligamina-cli",
    ],
    cwd: process.cwd(),
    env: { ...process.env, CARGO_NET_OFFLINE: "true" },
  });
  const client = new LigaminaMcpClient();
  try {
    await client.connect(transport);
    const tools = await client.listTools();
    assert.ok(tools.tools.some((tool) => tool.name === "_ligamina_ping"));

    const ping = await client.callTool("_ligamina_ping");
    assert.deepEqual(ping.content, [{ type: "text", text: "pong" }]);
    assert.equal(ping.isError, false);
  } finally {
    await client.close();
  }
});
