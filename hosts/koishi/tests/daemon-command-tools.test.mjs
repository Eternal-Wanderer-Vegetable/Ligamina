import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const repositoryRoot = fileURLToPath(new URL("../../../", import.meta.url));
const koishiDirectory = fileURLToPath(new URL("../", import.meta.url));

function token() {
  return randomBytes(24).toString("hex");
}

async function startDaemon(work, commandTools) {
  await mkdir(work, { recursive: true });
  const clientToken = token();
  const config = {
    listen: "127.0.0.1:0",
    database_path: path.join(work, "core.sqlite3"),
    admin_token: token(),
    mcp_client_id: "agent",
    clients: [{ id: "agent", token: clientToken, capabilities: ["ligamina.koishi.math.add"] }],
    // A Gateway is configured but never leased: command tool replies must not need it.
    gateways: [{ id: "unused-gateway", client_id: "agent", account_id: "10001", registration_token: token() }],
    hosts: [{
      id: "koishi",
      program: process.execPath,
      args: ["dist/worker.js"],
      current_dir: koishiDirectory,
      environment: {
        LIGAMINA_KOISHI_PLUGINS: pathToFileURL(path.join(koishiDirectory, "dist/tool-smoke-plugin.js")).href,
        LIGAMINA_KOISHI_DATA_DIR: path.join(work, "koishi-data"),
      },
      max_restarts: 0,
      startup_timeout_ms: 30000,
    }],
    ...(commandTools === undefined ? {} : { command_tools: commandTools }),
  };
  const configPath = path.join(work, "daemon.json");
  await writeFile(configPath, JSON.stringify(config));
  const executable = path.join(repositoryRoot, "target", "debug", process.platform === "win32" ? "ligamina-cli.exe" : "ligamina-cli");
  assert.ok(existsSync(executable), `missing daemon executable at ${executable}`);
  const child = spawn(executable, ["daemon", "--config", configPath], { cwd: repositoryRoot, stdio: ["ignore", "pipe", "pipe"] });
  child.stderr.resume();
  const lines = createInterface({ input: child.stdout });
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("daemon did not become ready")), 60_000);
    child.once("exit", (code) => reject(new Error(`daemon exited early with ${code}`)));
    lines.on("line", (line) => {
      try {
        const value = JSON.parse(line);
        if (value.ready) {
          clearTimeout(timer);
          resolve(value);
        }
      } catch {}
    });
  });
  return { child, listen: ready.listen, clientToken };
}

async function stopDaemon(child) {
  if (child.exitCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.kill();
  await exited;
}

async function connect(listen, clientToken) {
  const transport = new StreamableHTTPClientTransport(new URL(`http://${listen}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${clientToken}` } },
  });
  const client = new Client({ name: "daemon-command-tools-acceptance", version: "0.1.0" }, { capabilities: {} });
  await client.connect(transport);
  return client;
}

async function waitForTool(client, name) {
  const until = Date.now() + 30_000;
  for (;;) {
    const names = (await client.listTools()).tools.map((tool) => tool.name);
    if (names.includes(name)) return names;
    if (Date.now() > until) assert.fail(`tool ${name} was not listed: ${names.join(", ")}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

test("daemon exposes Koishi commands over Streamable HTTP MCP only when command_tools is enabled", async () => {
  execFileSync("cargo", ["build", "--quiet", "--package", "ligamina-cli"], { cwd: repositoryRoot, stdio: "inherit" });
  const work = await mkdtemp(path.join(tmpdir(), "ligamina-daemon-tools-"));
  try {
    const enabled = await startDaemon(path.join(work, "enabled"), { enabled: true, allow: ["cmd.koishi.ligamina.echo"] });
    try {
      const client = await connect(enabled.listen, enabled.clientToken);
      try {
        const names = await waitForTool(client, "cmd.koishi.ligamina.echo");
        assert.ok(!names.includes("cmd.koishi.ligamina.twice"), "allowlist hides other commands");
        const result = await client.callTool({ name: "cmd.koishi.ligamina.echo", arguments: { arguments: "-u over http" } });
        assert.equal(result.isError, false);
        assert.equal(result.content[0].text, "OVER HTTP");
        assert.equal(result.structuredContent.messages[0].text, "OVER HTTP");
      } finally {
        await client.close();
      }

      const unauthorized = new Client({ name: "no-token", version: "0.1.0" }, { capabilities: {} });
      await assert.rejects(() => unauthorized.connect(new StreamableHTTPClientTransport(new URL(`http://${enabled.listen}/mcp`))));
    } finally {
      await stopDaemon(enabled.child);
    }

    const disabled = await startDaemon(path.join(work, "disabled"), undefined);
    try {
      const client = await connect(disabled.listen, disabled.clientToken);
      try {
        await waitForTool(client, "ligamina.koishi.math.add");
        const names = (await client.listTools()).tools.map((tool) => tool.name);
        assert.ok(names.every((name) => !name.startsWith("cmd.")), "command tools stay hidden by default");
      } finally {
        await client.close();
      }
    } finally {
      await stopDaemon(disabled.child);
    }
  } finally {
    await rm(work, { recursive: true, force: true });
  }
});
