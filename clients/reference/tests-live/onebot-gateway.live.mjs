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
const nonebot = path.join(root, "hosts", "nonebot");
const astrbot = path.join(root, "hosts", "astrbot");
const executable = path.join(root, "target", "debug", process.platform === "win32" ? "ligamina-cli.exe" : "ligamina-cli");
const ACCOUNT = "10001";
const token = () => randomBytes(24).toString("hex");

function hostConfig(work, ecosystem) {
  const common = {
    id: ecosystem,
    max_restarts: 0,
    startup_timeout_ms: 60000,
    operation_grants: [{ id: `${ecosystem}-send`, client_id: "bot", account_id: ACCOUNT, operations: ["send_message"] }],
  };
  if (ecosystem === "nonebot") {
    return {
      ...common,
      program: "uv",
      args: ["run", "--locked", "--project", nonebot, "--python", "3.12.14", "python", "-m", "ligamina_nonebot.worker"],
      current_dir: root,
      environment: {
        PYTHONPATH: nonebot,
        LIGAMINA_NONEBOT_PLUGINS: "ligamina_nonebot.tool_smoke_plugin",
      },
    };
  }
  if (ecosystem === "astrbot") {
    return {
      ...common,
      program: "uv",
      args: ["run", "--locked", "--project", astrbot, "--python", "3.12.14", "python", "-m", "ligamina_astrbot.worker"],
      current_dir: root,
      environment: {
        PYTHONPATH: astrbot,
        LIGAMINA_ASTRBOT_PLUGINS: path.join(astrbot, "tests", "fixtures", "ligamina_smoke"),
        LIGAMINA_ASTRBOT_DATA_DIR: path.join(work, "astrbot-data"),
      },
    };
  }
  return {
    ...common,
    program: process.execPath,
    args: ["dist/worker.js"],
    current_dir: koishi,
    environment: {
      LIGAMINA_KOISHI_PLUGINS: pathToFileURL(path.join(koishi, "dist/tool-smoke-plugin.js")).href,
      LIGAMINA_KOISHI_DATA_DIR: path.join(work, "koishi-data"),
    },
  };
}

function eventText(ecosystem, text) {
  if (ecosystem === "nonebot") return `/ligamina.echo ${text}`;
  if (ecosystem === "astrbot") return `/ligamina_echo ${text}`;
  return `ligamina.echo -u ${text}`;
}

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

async function startDaemon(work, options = {}) {
  const ecosystem = options.ecosystem ?? "koishi";
  const clientToken = options.clientToken ?? token();
  const registrationToken = options.registrationToken ?? token() + token();
  const config = {
    listen: `127.0.0.1:${options.port ?? 0}`,
    database_path: path.join(work, "core.sqlite3"),
    admin_token: token(),
    mcp_client_id: "bot",
    clients: [{
      id: "bot",
      token: clientToken,
      ...(options.readGrant ? { operation_grants: [{ id: "bot-read", account_id: ACCOUNT, operations: ["send_message"] }] } : {}),
    }],
    gateways: [{ id: "qq-main", client_id: "bot", account_id: ACCOUNT, registration_token: registrationToken }],
    hosts: [hostConfig(work, ecosystem)],
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
    await stopDaemon(daemon);
    onebot.server.close();
    await rm(work, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});

async function freePort() {
  const { createServer: createNetServer } = await import("node:net");
  return new Promise((resolve) => {
    const server = createNetServer().listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitFor(predicate, timeoutMs, what) {
  const deadline = Date.now() + timeoutMs;
  while (!(await predicate())) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

function stopDaemon(daemon) {
  return new Promise((resolve) => {
    if (daemon.child.exitCode !== null) return resolve();
    daemon.child.once("exit", () => resolve());
    daemon.child.kill();
  });
}

// P6-02: Core is killed while the Gateway is in the middle of a platform send. The send
// is not repeated, the restarted Core keeps that operation unknown, the Gateway hands
// its late outcome to onUnreported, re-leases and serves the next message.
test("a Core crash during a send leaves it unknown, never re-sent, and the Gateway recovers", { skip: !existsSync(executable) || !existsSync(path.join(koishi, "dist/worker.js")) }, async () => {
  const work = await mkdtemp(path.join(tmpdir(), "ligamina-onebot-restart-"));
  const sends = [];
  let releaseFirst;
  const firstHeld = new Promise((resolve) => { releaseFirst = resolve; });
  let next = 8000;
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", async () => {
      const action = request.url.slice(1);
      const parsed = JSON.parse(body || "{}");
      if (action.startsWith("send_")) {
        sends.push(parsed.message.map((segment) => segment.data.text).join(""));
        if (sends.length === 1) await firstHeld;
      }
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ status: "ok", retcode: 0, data: { message_id: next++ } }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const onebotUrl = `http://127.0.0.1:${server.address().port}`;
  const port = await freePort();
  const credentials = { port, clientToken: token(), registrationToken: token() + token(), readGrant: true };
  let daemon = await startDaemon(work, credentials);
  const stop = new AbortController();
  const errors = [];
  const unreported = [];
  let running;
  try {
    const client = new LigaminaClient({ baseUrl: daemon.baseUrl, token: credentials.clientToken });
    const gateway = new OneBotGateway({
      core: client,
      gatewayId: "qq-main",
      registrationToken: credentials.registrationToken,
      onebot: { baseUrl: onebotUrl },
      actionTimeoutMs: 60_000,
      onError: (error) => errors.push(error),
      onUnreported: (record) => unreported.push(record),
    });
    running = gateway.run(stop.signal, 50);
    const message = (id, text) => toEventEnvelope({
      post_type: "message", message_type: "group", self_id: Number(ACCOUNT), group_id: 42, user_id: 7,
      message_id: id, time: Math.floor(Date.now() / 1000),
      message: [{ type: "text", data: { text: `ligamina.echo -u ${text}` } }],
    }, "koishi");

    await client.submitEvent(message(601, "first"));
    await waitFor(() => sends.length === 1, 30_000, "the first send to reach OneBot");

    // Crash Core while the send is in flight, then start a new Core on the same ledger.
    await stopDaemon(daemon);
    daemon = await startDaemon(work, credentials);
    releaseFirst();

    await waitFor(() => unreported.length === 1, 30_000, "the late outcome to be dropped");
    assert.equal(unreported[0].status, "succeeded", "OneBot did accept the held send");
    const query = new URLSearchParams({ account_id: ACCOUNT, operation: "send_message" });
    const record = await fetch(`${daemon.baseUrl}/v1/operations/${encodeURIComponent(unreported[0].operation_id)}?${query}`, {
      headers: { authorization: `Bearer ${credentials.clientToken}` },
    }).then((response) => response.json());
    assert.equal(record.status, "unknown", `ledger keeps the crashed send unknown: ${JSON.stringify(record)}`);
    assert.equal(record.result?.error, "core_epoch_changed");

    await client.submitEvent(message(602, "second"));
    await waitFor(() => sends.length === 2, 30_000, "the second send after restart");
    // Give a stray replay time to show up, then check there was none.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    assert.deepEqual(sends, ["FIRST", "SECOND"]);
    assert.equal(gateway.hasPendingReport, false);
    // The Gateway was inside the held send for the whole outage, so the only symptom is
    // the rejected report (stale lease from the previous Core epoch).
    assert.match(unreported[0].reason, /Core rejected the report/);
    assert.deepEqual(errors, [], "no loop error: the rejected report is an outcome, not a failure");
  } finally {
    stop.abort();
    releaseFirst();
    await running?.catch(() => {});
    await stopDaemon(daemon);
    server.closeAllConnections?.();
    server.close();
    await rm(work, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});

// P6-01 (feat-030): three copies of the same OneBot event are redelivered to each
// framework Host: while the reply is in flight, after completion, and after a Core
// restart on the same ledger. Core accepts it once and the plugin reply reaches the
// local OneBot endpoint exactly once. A different payload reusing the event ID is refused.
test("OneBot event redelivery is deduplicated across all three Hosts", { skip: !existsSync(executable) || !existsSync(path.join(koishi, "dist/worker.js")) }, async () => {
  for (const ecosystem of ["koishi", "nonebot", "astrbot"]) {
    const work = await mkdtemp(path.join(tmpdir(), `ligamina-onebot-duplicate-${ecosystem}-`));
    const sends = [];
    let releaseFirst;
    const firstHeld = new Promise((resolve) => { releaseFirst = resolve; });
    let next = 9500;
    const server = createServer((request, response) => {
      let body = "";
      request.on("data", (chunk) => { body += chunk; });
      request.on("end", async () => {
        const parsed = JSON.parse(body || "{}");
        if (request.url.startsWith("/send_")) {
          sends.push(parsed.message.map((segment) => segment.data.text).join(""));
          if (sends.length === 1) await firstHeld;
        }
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ status: "ok", retcode: 0, data: { message_id: next++ } }));
      });
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const onebotUrl = `http://127.0.0.1:${server.address().port}`;
    const port = await freePort();
    const credentials = { port, clientToken: token(), registrationToken: token() + token(), readGrant: true, ecosystem };
    let daemon = await startDaemon(work, credentials);
    const stop = new AbortController();
    let running;
    const startGateway = (client, signal) => new OneBotGateway({
      core: client,
      gatewayId: "qq-main",
      registrationToken: credentials.registrationToken,
      onebot: { baseUrl: onebotUrl },
      actionTimeoutMs: 60_000,
      onError: () => {},
    }).run(signal, 50);
    const onebotEvent = (text) => ({
      post_type: "message", message_type: "group", self_id: Number(ACCOUNT), group_id: 42, user_id: 7,
      message_id: 801, time: 1_700_000_000,
      message: [{ type: "text", data: { text: eventText(ecosystem, text) } }],
    });
    const eventStatus = async (eventId) => {
      const query = new URLSearchParams({ platform: "onebot.v11", account_id: ACCOUNT, conversation_kind: "group", conversation_id: "42" });
      return fetch(`${daemon.baseUrl}/v1/events/${encodeURIComponent(eventId)}?${query}`, {
        headers: { authorization: `Bearer ${credentials.clientToken}` },
      }).then((response) => response.json());
    };
    try {
      let client = new LigaminaClient({ baseUrl: daemon.baseUrl, token: credentials.clientToken });
      running = startGateway(client, stop.signal);
      const envelope = toEventEnvelope(onebotEvent("once"), ecosystem);
      // The same OneBot event converts to the same envelope, so redelivery is detectable.
      assert.deepEqual(toEventEnvelope(onebotEvent("once"), ecosystem), envelope);

      const dispositions = [];
      dispositions.push((await client.submitEvent(envelope)).disposition);
      await waitFor(() => sends.length === 1, 60_000, `${ecosystem} reply to reach OneBot`);
      // Copy 2 arrives while the reply is still in flight.
      dispositions.push((await client.submitEvent(envelope)).disposition);
      releaseFirst();
      await waitFor(async () => (await eventStatus(envelope.event_id)).status === "completed", 60_000, `${ecosystem} event to complete`);
      // Copy 3 arrives after completion.
      dispositions.push((await client.submitEvent(envelope)).disposition);

      // Copy 4 arrives after Core restarts on the same ledger.
      stop.abort();
      await running.catch(() => {});
      await stopDaemon(daemon);
      daemon = await startDaemon(work, credentials);
      client = new LigaminaClient({ baseUrl: daemon.baseUrl, token: credentials.clientToken });
      const restartStop = new AbortController();
      running = startGateway(client, restartStop.signal);
      try {
        const fourth = await client.submitEvent(envelope);
        dispositions.push(fourth.disposition);
        assert.equal(fourth.event.status, "completed", `${ecosystem} restarted Core retains the original outcome`);

        // A different payload under the same OneBot message ID is a conflict, not a new event.
        await assert.rejects(client.submitEvent(toEventEnvelope(onebotEvent("tampered"), ecosystem)));
        await new Promise((resolve) => setTimeout(resolve, 1_500));
      } finally {
        restartStop.abort();
        await running.catch(() => {});
      }
      assert.deepEqual(dispositions, ["accepted", "duplicate", "duplicate", "duplicate"], `${ecosystem} dispositions`);
      assert.deepEqual(sends, ["ONCE"], `${ecosystem} must send exactly once for four deliveries`);
      assert.equal((await eventStatus(envelope.event_id)).status, "completed");
    } finally {
      stop.abort();
      releaseFirst();
      await running?.catch(() => {});
      await stopDaemon(daemon);
      server.closeAllConnections?.();
      server.close();
      await rm(work, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  }
});

// P6-02: the Gateway process dies in the middle of a send. A replacement Gateway takes
// the lease over; Core marks the abandoned operation unknown (never re-offers it), and
// the replacement serves new messages. The abandoned send is attempted exactly once.
test("a Gateway crash during a send leaves it unknown and a replacement Gateway takes over", { skip: !existsSync(executable) || !existsSync(path.join(koishi, "dist/worker.js")) }, async () => {
  const work = await mkdtemp(path.join(tmpdir(), "ligamina-onebot-takeover-"));
  const sends = [];
  let releaseFirst;
  const firstHeld = new Promise((resolve) => { releaseFirst = resolve; });
  let next = 9000;
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", async () => {
      const parsed = JSON.parse(body || "{}");
      if (request.url.startsWith("/send_")) {
        sends.push(parsed.message.map((segment) => segment.data.text).join(""));
        if (sends.length === 1) await firstHeld;
      }
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ status: "ok", retcode: 0, data: { message_id: next++ } }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const onebotUrl = `http://127.0.0.1:${server.address().port}`;
  const daemon = await startDaemon(work, { readGrant: true });
  const stopA = new AbortController();
  const stopB = new AbortController();
  const unreportedA = [];
  let runningA;
  let runningB;
  try {
    const client = new LigaminaClient({ baseUrl: daemon.baseUrl, token: daemon.clientToken });
    const gatewayOptions = {
      core: client,
      gatewayId: "qq-main",
      registrationToken: daemon.registrationToken,
      onebot: { baseUrl: onebotUrl },
      actionTimeoutMs: 60_000,
      onError: () => {},
    };
    const gatewayA = new OneBotGateway({ ...gatewayOptions, onUnreported: (record) => unreportedA.push(record) });
    runningA = gatewayA.run(stopA.signal, 50);
    const message = (id, text) => toEventEnvelope({
      post_type: "message", message_type: "group", self_id: Number(ACCOUNT), group_id: 42, user_id: 7,
      message_id: id, time: Math.floor(Date.now() / 1000),
      message: [{ type: "text", data: { text: `ligamina.echo -u ${text}` } }],
    }, "koishi");

    await client.submitEvent(message(701, "first"));
    await waitFor(() => sends.length === 1, 30_000, "the first send to reach OneBot");

    // Gateway A is now wedged inside the send, as if its process had died. Start B.
    const unreportedB = [];
    const gatewayB = new OneBotGateway({ ...gatewayOptions, onUnreported: (record) => unreportedB.push(record) });
    runningB = gatewayB.run(stopB.signal, 50);

    await client.submitEvent(message(702, "second"));
    await waitFor(() => sends.length === 2, 30_000, "the replacement Gateway to send");

    // A finally comes back: its late report is refused because B replaced its lease.
    stopA.abort();
    releaseFirst();
    await runningA;
    assert.equal(unreportedA.length, 1);
    const query = new URLSearchParams({ account_id: ACCOUNT, operation: "send_message" });
    const record = await fetch(`${daemon.baseUrl}/v1/operations/${encodeURIComponent(unreportedA[0].operation_id)}?${query}`, {
      headers: { authorization: `Bearer ${daemon.clientToken}` },
    }).then((response) => response.json());
    assert.equal(record.status, "unknown", `abandoned send stays unknown: ${JSON.stringify(record)}`);
    assert.equal(record.result?.error, "lease_replaced");

    await new Promise((resolve) => setTimeout(resolve, 1_000));
    assert.deepEqual(sends, ["FIRST", "SECOND"], "no replay of the abandoned send");
    assert.deepEqual(unreportedB, []);
  } finally {
    stopA.abort();
    stopB.abort();
    releaseFirst();
    await runningA?.catch(() => {});
    await runningB?.catch(() => {});
    await stopDaemon(daemon);
    server.closeAllConnections?.();
    server.close();
    await rm(work, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});
