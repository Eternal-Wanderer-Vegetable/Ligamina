// Live end-to-end check of the bot-neutral OneBot v11 Gateway (TOOL-05).
//
// Starts the real ligamina-cli daemon with a Koishi Host running a self-authored
// plugin, a fake OneBot v11 HTTP server standing in for the bot's platform
// connection, and the reference OneBotGateway in between. A OneBot group message
// event goes in; the plugin's reply must come out as a OneBot send_group_msg.
//
// Requires `cargo build -p ligamina-cli` and `npm ci && npm run build` in hosts/koishi.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { LigaminaClient } from "../dist/client.js";
import { OneBotGateway, toEventEnvelope } from "../dist/onebot-gateway.js";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const koishi = path.join(root, "hosts", "koishi");
const executable = path.join(root, "target", "debug", process.platform === "win32" ? "ligamina-cli.exe" : "ligamina-cli");
const ACCOUNT = "10001";
const token = () => randomBytes(24).toString("hex");

function fakeOneBot() {
  const actions = [];
  let next = 7000;
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      actions.push({ action: request.url.slice(1), auth: request.headers.authorization, body: JSON.parse(body || "{}") });
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ status: "ok", retcode: 0, data: { message_id: next++ } }));
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, actions, url: `http://127.0.0.1:${server.address().port}` })));
}

async function startDaemon(work) {
  const clientToken = token();
  const registrationToken = token() + token();
  const config = {
    listen: "127.0.0.1:0",
    database_path: path.join(work, "core.sqlite3"),
    admin_token: token(),
    mcp_client_id: "bot",
    clients: [{ id: "bot", token: clientToken }],
    gateways: [{ id: "qq-main", client_id: "bot", account_id: ACCOUNT, registration_token: registrationToken }],
    hosts: [{
      id: "koishi",
      program: process.execPath,
      args: ["dist/worker.js"],
      current_dir: koishi,
      environment: {
        LIGAMINA_KOISHI_PLUGINS: pathToFileURL(path.join(koishi, "dist/tool-smoke-plugin.js")).href,
        LIGAMINA_KOISHI_DATA_DIR: path.join(work, "koishi-data"),
      },
      max_restarts: 0,
      startup_timeout_ms: 30000,
      operation_grants: [{ id: "koishi-send", client_id: "bot", account_id: ACCOUNT, operations: ["send_message"] }],
    }],
  };
  const configPath = path.join(work, "daemon.json");
  await writeFile(configPath, JSON.stringify(config));
  const child = spawn(executable, ["daemon", "--config", configPath], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  child.stderr.resume();
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("daemon did not become ready")), 60_000);
    child.once("exit", (code) => reject(new Error(`daemon exited early with ${code}`)));
    createInterface({ input: child.stdout }).on("line", (line) => {
      try {
        const value = JSON.parse(line);
        if (value.ready) { clearTimeout(timer); resolve(value); }
      } catch {}
    });
  });
  return { child, baseUrl: `http://${ready.listen}`, clientToken, registrationToken };
}

test("a OneBot group message reaches a Koishi plugin and its reply goes out through OneBot", { skip: !existsSync(executable) || !existsSync(path.join(koishi, "dist/worker.js")) }, async () => {
  const work = await mkdtemp(path.join(tmpdir(), "ligamina-onebot-gateway-"));
  const onebot = await fakeOneBot();
  const daemon = await startDaemon(work);
  const stop = new AbortController();
  try {
    const client = new LigaminaClient({ baseUrl: daemon.baseUrl, token: daemon.clientToken });
    const gateway = new OneBotGateway({
      core: client,
      gatewayId: "qq-main",
      registrationToken: daemon.registrationToken,
      onebot: { baseUrl: onebot.url, accessToken: "onebot-secret" },
    });
    const running = gateway.run(stop.signal, 50);

    const envelope = toEventEnvelope({
      post_type: "message", message_type: "group", self_id: Number(ACCOUNT), group_id: 42, user_id: 7,
      message_id: 555, time: Math.floor(Date.now() / 1000), sender: { nickname: "Ada" },
      message: [{ type: "text", data: { text: "ligamina.echo -u hello onebot" } }],
    }, "koishi");
    await client.submitEvent(envelope);

    const deadline = Date.now() + 30_000;
    while (!onebot.actions.some((action) => action.action === "send_group_msg") && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    const send = onebot.actions.find((action) => action.action === "send_group_msg");
    assert.ok(send, `no OneBot send observed: ${JSON.stringify(onebot.actions)}`);
    assert.equal(send.auth, "Bearer onebot-secret");
    assert.deepEqual(send.body, { group_id: 42, message: [{ type: "text", data: { text: "HELLO ONEBOT" } }] });

    // The Core recorded the Gateway's receipt for the operation.
    const until = Date.now() + 10_000;
    let status;
    do {
      const query = new URLSearchParams({ platform: "onebot.v11", account_id: ACCOUNT, conversation_kind: "group", conversation_id: "42" });
      status = await fetch(`${daemon.baseUrl}/v1/events/${encodeURIComponent(envelope.event_id)}?${query}`, {
        headers: { authorization: `Bearer ${daemon.clientToken}` },
      }).then((response) => response.json());
      if (status.status === "completed") break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    } while (Date.now() < until);
    assert.equal(status.status, "completed");
    stop.abort();
    await running;
  } finally {
    stop.abort();
    daemon.child.kill();
    onebot.server.close();
    await rm(work, { recursive: true, force: true });
  }
});
