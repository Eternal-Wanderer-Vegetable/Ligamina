import assert from "node:assert/strict";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { LigaminaMcpClient } from "../dist/mcp-client.js";

const [baseUrl, token, nonebotCapability, koishiCapability] = process.argv.slice(2);
if (!baseUrl || !token || !nonebotCapability || !koishiCapability) {
  throw new Error("usage: node mcp-http-smoke.mjs <base-url> <token> <nonebot-capability> <koishi-capability>");
}

const transport = new StreamableHTTPClientTransport(new URL(`${baseUrl.replace(/\/+$/, "")}/mcp`), {
  requestInit: { headers: { authorization: `Bearer ${token}` } },
});
const client = new LigaminaMcpClient("ligamina-g1-smoke", "0.1.0");
try {
  await client.connect(transport);
  const tools = await client.listTools();
  const names = new Set(tools.tools.map((tool) => tool.name));
  assert.ok(names.has(nonebotCapability), `missing NoneBot capability ${nonebotCapability}`);
  assert.ok(names.has(koishiCapability), `missing Koishi capability ${koishiCapability}`);

  const invocations = [];
  for (const capability of [nonebotCapability, koishiCapability]) {
    const result = await client.callTool(capability, { left: 19, right: 23 });
    assert.equal(result.isError, false);
    assert.equal(result.structuredContent?.result, 42);
    invocations.push({ capability, result: result.structuredContent });
  }
  process.stdout.write(`${JSON.stringify({ protocol: "2025-11-25", tools: [...names].sort(), invocations })}\n`);
} finally {
  await client.close();
}
