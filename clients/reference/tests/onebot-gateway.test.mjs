import assert from "node:assert/strict";
import test from "node:test";
import { LigaminaClient } from "../dist/client.js";
import { OneBotGateway, fromOneBotMessage, toEventEnvelope, toOneBotMessage } from "../dist/onebot-gateway.js";

const CORE = "http://127.0.0.1:8765";
const ONEBOT = "http://127.0.0.1:3000";
const lease = { lease_id: "lease-1", token: "lease-secret", lease_epoch: 1, core_epoch: "e1", expires_at_ms: 10_000_000 };

function operation(kind = "group") {
  return {
    key: { client_id: "bot", account_id: "10001", operation: "send_message", operation_id: "op-1" },
    fingerprint: "fp-1",
    operation_payload: {
      account_id: "10001",
      operation: "send_message",
      target: { kind, id: "42" },
      params: { message: [{ type: "mention", user_id: "7" }, { type: "text", text: " hi" }] },
    },
  };
}

function harness({ claims, onebot }) {
  const calls = { core: [], onebot: [] };
  const fetch = async (url, init) => {
    if (url.startsWith(ONEBOT)) {
      calls.onebot.push({ action: url.slice(ONEBOT.length + 1), body: JSON.parse(init.body) });
      return onebot(url.slice(ONEBOT.length + 1));
    }
    calls.core.push({ url, body: init.body ? JSON.parse(init.body) : undefined, auth: init.headers.get("authorization") });
    if (url.endsWith("/lease")) return Response.json(lease);
    if (url.endsWith("/actions/claim")) return Response.json(claims.shift() ?? null);
    return Response.json({ ok: true });
  };
  const gateway = new OneBotGateway({
    core: new LigaminaClient({ baseUrl: CORE, token: "unused", fetch }),
    gatewayId: "qq-main",
    registrationToken: "registration-secret",
    onebot: { baseUrl: ONEBOT, accessToken: "onebot-secret" },
    fetch,
    now: () => 1_000,
  });
  return { gateway, calls };
}

test("a claimed send is performed through OneBot and reported with the lease credential", async () => {
  const { gateway, calls } = harness({
    claims: [operation("group")],
    onebot: () => Response.json({ status: "ok", retcode: 0, data: { message_id: 123 } }),
  });
  assert.equal(await gateway.pollOnce(), true);
  assert.deepEqual(calls.onebot, [{
    action: "send_group_msg",
    body: { group_id: 42, message: [{ type: "at", data: { qq: "7" } }, { type: "text", data: { text: " hi" } }] },
  }]);
  const report = calls.core.at(-1);
  assert.match(report.url, /\/v1\/gateways\/qq-main\/actions\/op-1\/result$/);
  assert.equal(report.auth, "Bearer lease-secret");
  assert.deepEqual(report.body.credential, lease);
  assert.equal(report.body.result.status, "succeeded");
  assert.equal(report.body.result.platform_message_id, "123");
  assert.equal(report.body.result.fingerprint, "fp-1");
  // The lease is reused while valid: one lease call for this poll.
  assert.equal(calls.core.filter((call) => call.url.endsWith("/lease")).length, 1);
});

test("private sends use send_private_msg", async () => {
  const { gateway, calls } = harness({
    claims: [operation("private")],
    onebot: () => Response.json({ status: "ok", retcode: 0, data: { message_id: 9 } }),
  });
  await gateway.pollOnce();
  assert.equal(calls.onebot[0].action, "send_private_msg");
  assert.equal(calls.onebot[0].body.user_id, 42);
});

test("forward nodes use standard send_group_msg node segments in original order", async () => {
  const claimed = operation("group");
  claimed.key.operation_id = "op-forward";
  claimed.operation_payload.params.message = [
    { type: "forward_node", user_id: "9000", nickname: "Alpha", content: "first" },
    {
      type: "forward_node",
      user_id: "9012",
      nickname: "Beta",
      content: [{ type: "text", text: "second" }, { type: "mention", user_id: "23" }],
    },
  ];
  const { gateway, calls } = harness({
    claims: [claimed],
    onebot: () => Response.json({ status: "ok", retcode: 0, data: { message_id: 778 } }),
  });

  await gateway.pollOnce();

  assert.deepEqual(calls.onebot, [{
    action: "send_group_msg",
    body: {
      group_id: 42,
      message: [
        { type: "node", data: { user_id: "9000", nickname: "Alpha", content: "first" } },
        {
          type: "node",
          data: {
            user_id: "9012",
            nickname: "Beta",
            content: [
              { type: "text", data: { text: "second" } },
              { type: "at", data: { qq: "23" } },
            ],
          },
        },
      ],
    },
  }]);
  assert.equal(calls.core.at(-1).body.result.status, "succeeded");
  assert.equal(calls.core.at(-1).body.result.platform_message_id, "778");
});

test("malformed forward nodes and private forward targets fail before OneBot is called", async () => {
  const cases = [
    { kind: "private", node: { type: "forward_node", user_id: "9", nickname: "A", content: "x" } },
    { kind: "group", node: { type: "forward_node", user_id: "not-numeric", nickname: "A", content: "x" } },
    { kind: "group", node: { type: "forward_node", user_id: "9", nickname: "A", content: [{ type: "video" }] } },
    { kind: "group", node: { type: "forward_node", user_id: "9", nickname: "A", content: [] } },
  ];

  for (const [index, item] of cases.entries()) {
    const claimed = operation(item.kind);
    claimed.key.operation_id = `op-invalid-forward-${index}`;
    claimed.operation_payload.params.message = [item.node];
    const { gateway, calls } = harness({
      claims: [claimed],
      onebot: () => assert.fail("invalid forward input must not reach OneBot"),
    });

    await gateway.pollOnce();

    assert.equal(calls.onebot.length, 0);
    assert.equal(calls.core.at(-1).body.result.status, "failed");
  }
});

test("uncertain outcomes are reported as unknown and never retried", async () => {
  const cases = [
    () => { throw new TypeError("network down"); },
    () => Response.json({ status: "async", retcode: 1, data: null }),
    () => Response.json({ status: "ok", retcode: 0, data: {} }),
    () => new Response("bad gateway", { status: 502 }),
  ];
  for (const onebot of cases) {
    const { gateway, calls } = harness({ claims: [operation()], onebot });
    await gateway.pollOnce();
    assert.equal(calls.onebot.length, 1, "exactly one OneBot attempt");
    assert.equal(calls.core.at(-1).body.result.status, "unknown");
  }
});

test("explicit OneBot failures are reported as failed", async () => {
  const { gateway, calls } = harness({
    claims: [operation()],
    onebot: () => Response.json({ status: "failed", retcode: 100, wording: "bot muted" }),
  });
  await gateway.pollOnce();
  const result = calls.core.at(-1).body.result;
  assert.equal(result.status, "failed");
  assert.match(result.error, /retcode 100: bot muted/);
});

test("an empty claim does nothing", async () => {
  const { gateway, calls } = harness({ claims: [], onebot: () => assert.fail("no OneBot call expected") });
  assert.equal(await gateway.pollOnce(), false);
  assert.equal(calls.onebot.length, 0);
});

test("OneBot message events become Core envelopes; non-message events are ignored", () => {
  const envelope = toEventEnvelope({
    post_type: "message", message_type: "group", self_id: 10001, group_id: 42, user_id: 7,
    message_id: 555, time: 1_700_000_000, sender: { nickname: "Ada" },
    message: [{ type: "at", data: { qq: "10001" } }, { type: "text", data: { text: " echo hi" } }],
  }, "koishi");
  assert.equal(envelope.platform, "onebot.v11");
  assert.equal(envelope.account_id, "10001");
  assert.deepEqual(envelope.conversation, { kind: "group", id: "42" });
  assert.equal(envelope.event_id, "onebot-10001-555");
  assert.equal(envelope.target_host, "koishi");
  assert.deepEqual(envelope.message, [{ type: "mention", user_id: "10001" }, { type: "text", text: " echo hi" }]);
  assert.deepEqual(envelope.extensions["onebot.v11"], { message_id: "555", sender: { nickname: "Ada" } });
  assert.equal(toEventEnvelope({ post_type: "notice" }, "koishi"), null);
});

test("segment mapping round-trips between Core and OneBot shapes", () => {
  const core = [
    { type: "text", text: "a" },
    { type: "mention", user_id: "7" },
    { type: "reply", message_id: "9" },
    { type: "image_ref", resource_ref: "https://example.invalid/x.png" },
  ];
  assert.deepEqual(fromOneBotMessage(toOneBotMessage(core)), core);
  assert.throws(() => toOneBotMessage([{ type: "video" }]), /unsupported message segment/);
});
