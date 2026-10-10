import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, LigaminaClient } from "../dist/client.js";
import { OperationConflictError, VirtualPlatform } from "../dist/virtual-platform.js";

test("reference client keeps tokens in authorization headers and encodes route IDs", async () => {
  const requests = [];
  const client = new LigaminaClient({
    baseUrl: "http://127.0.0.1:8765/",
    token: "client-secret",
    fetch: async (url, init) => {
      requests.push({ url, init });
      return Response.json({ accepted: true }, { status: 202 });
    },
  });
  await client.setHostDesired("worker/A", { enabled: true });
  assert.equal(requests[0].url, "http://127.0.0.1:8765/v1/hosts/worker%2FA/desired");
  assert.equal(requests[0].init.headers.get("authorization"), "Bearer client-secret");
  assert.equal(JSON.stringify(requests[0]).includes("client-secret"), false);
});

test("gateway operations use the registration token and rotated lease token for separate calls", async () => {
  const requests = [];
  const client = new LigaminaClient({
    baseUrl: "http://127.0.0.1:8765",
    token: "client-token",
    fetch: async (url, init) => {
      requests.push({ url, init });
      return Response.json({ ok: true });
    },
  });
  await client.acquireGatewayLease("gateway A", "registration-secret", 60_000);
  await client.claimGatewayAction("gateway A", "lease-secret");
  assert.equal(requests[0].init.headers.get("authorization"), "Bearer registration-secret");
  assert.equal(requests[1].init.headers.get("authorization"), "Bearer lease-secret");
  assert.match(requests[1].url, /gateway%20A/);
});

test("reference client maps structured HTTP errors without exposing credentials", async () => {
  const client = new LigaminaClient({
    baseUrl: "http://127.0.0.1:8765",
    token: "sensitive-token",
    fetch: async () =>
      Response.json(
        { error: { code: "operation_conflict", message: "different payload" } },
        { status: 409 },
      ),
  });
  await assert.rejects(client.status(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, "operation_conflict");
    assert.equal(error.message.includes("sensitive-token"), false);
    return true;
  });
});

test("virtual platform sends idempotently and serves scoped history with message segments intact", () => {
  const platform = new VirtualPlatform();
  const operation = {
    operation_id: "op-1",
    trace_id: "trace-1",
    account_id: "account-1",
    operation: "send_message",
    target: { kind: "group", id: "room-1" },
    params: {
      message: [
        { type: "reply", message_id: "source-1" },
        { type: "mention", user_id: "user-1" },
        { type: "text", text: "hello" },
      ],
    },
  };
  const result = platform.send(operation);
  const retry = platform.send({ ...operation, trace_id: "trace-retry" });
  assert.equal(retry.platform_message_id, result.platform_message_id);
  assert.equal(platform.history("account-1", operation.target).length, 1);
  assert.deepEqual(
    platform.getMessage("account-1", operation.target, result.platform_message_id)?.message,
    operation.params.message,
  );
  assert.throws(
    () => platform.send({ ...operation, params: { message: [{ type: "text", text: "changed" }] } }),
    OperationConflictError,
  );
  assert.equal(platform.history("account-2", operation.target).length, 0);
});
