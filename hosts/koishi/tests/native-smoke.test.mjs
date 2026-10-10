import assert from "node:assert/strict";
import test from "node:test";
import { Context } from "@koishijs/core";

test("the locked Koishi core starts and shuts down without a server", async () => {
  const lifecycle = { loaded: false, started: false, stopped: false };
  const app = new Context({});
  app.plugin((ctx) => {
    lifecycle.loaded = true;
    ctx.command("ligamina.smoke").action(() => "ok");
    ctx.on("ready", () => (lifecycle.started = true));
    ctx.on("dispose", () => (lifecycle.stopped = true));
  });

  assert.equal(lifecycle.loaded, true);
  await app.start();
  assert.equal(lifecycle.started, true);
  await app.stop();
  assert.equal(lifecycle.stopped, true);
});
