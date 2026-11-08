// P6-02: OneBot Gateway fault containment and recovery.
//
// Every case checks the two invariants that matter for delivery correctness:
// a platform send is attempted at most once per claimed operation, and one bad
// operation or one Core/OneBot outage never stops the Gateway.
import assert from "node:assert/strict";
import test from "node:test";
import { LigaminaClient } from "../dist/client.js";
import { OneBotGateway } from "../dist/onebot-gateway.js";

const CORE = "http://127.0.0.1:8765";
const ONEBOT = "http://127.0.0.1:3000";

let leaseSeq = 0;
function freshLease(expires = 10_000_000) {
  leaseSeq += 1;
  return { lease_id: `lease-${leaseSeq}`, token: `lease-secret-${leaseSeq}`, lease_epoch: leaseSeq, core_epoch: "e1", expires_at_ms: expires };
}

function op(id, params = { message: [{ type: "text", text: "hi" }] }, operation = "send_message") {
  return {
    key: { client_id: "bot", account_id: "10001", operation, operation_id: id },
    fingerprint: `fp-${id}`,
    operation_payload: { account_id: "10001", operation, target: { kind: "group", id: "42" }, params },
  };
}

const ok = (data = { message_id: 1 }) => Response.json({ status: "ok", retcode: 0, data });
const denied = (code) => Response.json({ error: { code, message: code } }, { status: 403 });

/**
 * `core` maps a route kind ("lease" | "claim" | "result") to a handler returning a
 * Response or throwing; defaults behave like a healthy Core.
 */
function harness({ claims = [], onebot = () => ok(), core = {}, now = () => 1_000, leaseExpires } = {}) {
  const calls = { lease: [], claim: [], result: [], onebot: [] };
  const errors = [];
  const unreported = [];
  const fetch = async (url, init) => {
    if (url.startsWith(ONEBOT)) {
      const action = url.slice(ONEBOT.length + 1);
      calls.onebot.push({ action, body: JSON.parse(init.body) });
      return onebot(action, calls.onebot.length);
    }
    const kind = url.endsWith("/lease") ? "lease" : url.endsWith("/actions/claim") ? "claim" : "result";
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls[kind].push({ url, body });
    if (core[kind]) {
      const response = await core[kind](calls[kind].length, body);
      if (response) return response;
    }
    if (kind === "lease") return Response.json(freshLease(leaseExpires));
    if (kind === "claim") return Response.json(claims.shift() ?? null);
    return Response.json({ ok: true });
  };
  const gateway = new OneBotGateway({
    core: new LigaminaClient({ baseUrl: CORE, token: "unused", fetch }),
    gatewayId: "qq-main",
    registrationToken: "registration-secret",
    onebot: { baseUrl: ONEBOT },
    fetch,
    now,
    onError: (error) => errors.push(error),
    onUnreported: (record) => unreported.push(record),
  });
  return { gateway, calls, errors, unreported };
}

/** Runs the loop until `done()` holds (or a time budget is exhausted), then aborts. */
async function runUntil(gateway, done, budgetMs = 3_000) {
  const controller = new AbortController();
  const loop = gateway.run(controller.signal, 0);
  const deadline = Date.now() + budgetMs;
  while (!done() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 2));
  controller.abort();
  await loop;
}

test("stage 1: an unsupported segment fails before sending and does not stop the next operation", async () => {
  const { gateway, calls } = harness({
    claims: [op("bad", { message: [{ type: "video" }] }), op("good")],
  });
  assert.equal(await gateway.pollOnce(), true);
  assert.equal(calls.onebot.length, 0, "nothing reaches OneBot");
  const bad = calls.result[0].body.result;
  assert.equal(bad.status, "failed");
  assert.match(bad.error, /^not sent: unsupported message segment video/);

  assert.equal(await gateway.pollOnce(), true);
  assert.equal(calls.onebot.length, 1);
  assert.equal(calls.result[1].body.result.status, "succeeded");
});

test("stage 1: malformed requests fail without a platform call", async () => {
  const cases = [
    op("no-message", {}),
    op("empty", { message: [] }),
    op("null-segment", { message: [null] }),
    op("bad-target", { message: [{ type: "text", text: "x" }] }),
    op("no-id", {}, "get_message"),
    op("other", {}, "delete_message"),
  ];
  cases[3].operation_payload.target = { kind: "channel", id: "1" };
  const { gateway, calls } = harness({ claims: [...cases] });
  for (let i = 0; i < cases.length; i += 1) assert.equal(await gateway.pollOnce(), true);
  assert.equal(calls.onebot.length, 0);
  assert.deepEqual(calls.result.map((call) => call.body.result.status), cases.map(() => "failed"));
  assert.ok(calls.result.every((call) => call.body.result.error.startsWith("not sent: ")));
});

test("stage 2: an uninterpretable reply is unknown for sends and failed for reads", async () => {
  const { gateway, calls } = harness({
    claims: [op("send"), op("read", { message_id: "5" }, "get_message"), op("self", {}, "get_self_info")],
    onebot: () => ok(null),
  });
  await gateway.pollOnce();
  await gateway.pollOnce();
  await gateway.pollOnce();
  assert.equal(calls.onebot.length, 3, "each action attempted exactly once");
  assert.deepEqual(calls.result.map((call) => call.body.result.status), ["unknown", "failed", "failed"]);
});

test("stage 2: a non-object OneBot body is unknown, not a crash", async () => {
  const { gateway, calls } = harness({ claims: [op("send")], onebot: () => Response.json(null) });
  assert.equal(await gateway.pollOnce(), true);
  assert.equal(calls.result[0].body.result.status, "unknown");
});

test("stage 3: a transient report failure re-reports the same outcome without re-sending", async () => {
  const { gateway, calls } = harness({
    claims: [op("op-1"), op("op-2")],
    core: { result: (n) => { if (n === 1) throw new TypeError("Core unreachable"); } },
  });
  await assert.rejects(gateway.pollOnce(), /Core unreachable/);
  assert.equal(gateway.hasPendingReport, true);

  assert.equal(await gateway.pollOnce(), true, "the retry poll flushes the pending report");
  assert.equal(gateway.hasPendingReport, false);
  assert.equal(calls.onebot.length, 1, "OneBot was called once");
  assert.equal(calls.claim.length, 1, "no new claim while a report was pending");
  assert.deepEqual(calls.result[1].body, calls.result[0].body, "identical report, same lease");

  await gateway.pollOnce();
  assert.equal(calls.onebot.length, 2);
  assert.equal(calls.result.at(-1).body.result.key.operation_id, "op-2");
});

test("stage 3: a server error on report is retried, a 403 is dropped and handed to onUnreported", async () => {
  const { gateway, calls, unreported } = harness({
    claims: [op("op-1"), op("op-2")],
    core: {
      result: (n) => n === 1
        ? Response.json({ error: { code: "internal", message: "boom" } }, { status: 500 })
        : n === 2 ? denied("result_denied") : undefined,
    },
  });
  await assert.rejects(gateway.pollOnce(), /boom/);
  assert.equal(gateway.hasPendingReport, true);
  assert.equal(await gateway.pollOnce(), true);
  assert.equal(gateway.hasPendingReport, false);
  assert.equal(unreported.length, 1);
  assert.equal(unreported[0].operation_id, "op-1");
  assert.equal(unreported[0].status, "succeeded");
  assert.equal(unreported[0].platform_message_id, "1");
  assert.match(unreported[0].reason, /result_denied/);

  // The denied lease is discarded: the next poll acquires a new one.
  await gateway.pollOnce();
  assert.equal(calls.lease.length, 2);
  assert.equal(calls.onebot.length, 2, "op-1 was never sent again");
});

test("stage 3: a report pending past lease expiry is dropped without calling Core", async () => {
  let clock = 1_000;
  const { gateway, calls, unreported } = harness({
    claims: [op("op-1")],
    now: () => clock,
    leaseExpires: 50_000,
    core: { result: (n) => { if (n === 1) throw new TypeError("Core unreachable"); } },
  });
  await assert.rejects(gateway.pollOnce());
  clock = 60_000;
  assert.equal(await gateway.pollOnce(), true);
  assert.equal(calls.result.length, 1, "no report attempted with an expired lease");
  assert.equal(unreported.length, 1);
  assert.match(unreported[0].reason, /lease expired/);
  assert.equal(calls.onebot.length, 1);
});

test("run(): Core outages back off and recover; every operation is sent once", async () => {
  const { gateway, calls, errors } = harness({
    claims: [op("op-1"), op("op-2")],
    core: {
      claim: (n) => { if (n <= 2) throw new TypeError("Core unreachable"); },
      result: (n) => { if (n === 1) throw new TypeError("Core unreachable"); },
    },
  });
  await runUntil(gateway, () => calls.result.filter((c) => c.body.result.key.operation_id === "op-2").length === 1);
  assert.equal(errors.length, 3, "two claim failures and one report failure were surfaced");
  assert.deepEqual(calls.onebot.map((c) => c.body.message[0].data.text), ["hi", "hi"]);
  assert.equal(calls.onebot.length, 2);
});

test("run(): a Core restart that invalidates the lease leads to a fresh lease", async () => {
  const { gateway, calls, errors } = harness({
    claims: [op("op-1")],
    core: { claim: (n) => (n === 1 ? denied("claim_denied") : undefined) },
  });
  await runUntil(gateway, () => calls.result.length === 1);
  assert.equal(errors.length, 1);
  assert.equal(calls.lease.length, 2);
  assert.notEqual(calls.claim[1].body.lease_id, calls.claim[0].body.lease_id, "claims after the denial use the new lease");
  assert.equal(calls.onebot.length, 1);
});

test("run(): a OneBot outage yields unknown outcomes and the loop keeps serving", async () => {
  const { gateway, calls, errors } = harness({
    claims: [op("op-1"), op("op-2")],
    onebot: (_action, n) => { if (n === 1) throw new TypeError("connect ECONNREFUSED"); return ok({ message_id: 9 }); },
  });
  await runUntil(gateway, () => calls.result.length === 2);
  assert.equal(errors.length, 0, "platform errors are outcomes, not loop errors");
  assert.deepEqual(calls.result.map((c) => c.body.result.status), ["unknown", "succeeded"]);
  assert.equal(calls.onebot.length, 2);
});

test("run(): abort ends the loop promptly even during backoff", async () => {
  const { gateway } = harness({ core: { lease: () => { throw new TypeError("Core unreachable"); } } });
  const controller = new AbortController();
  const loop = gateway.run(controller.signal, 60_000);
  await new Promise((resolve) => setImmediate(resolve));
  const started = Date.now();
  controller.abort();
  await loop;
  assert.ok(Date.now() - started < 1_000);
});
