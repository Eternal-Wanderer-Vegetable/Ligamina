import { createInterface } from "node:readline";

const pending = new Map();
let nextRequestId = 1;
let writeQueue = Promise.resolve();

console.error("node worker diagnostic on stderr");

function send(packet) {
  const frame = `${JSON.stringify(packet)}\n`;
  writeQueue = writeQueue.then(
    () => new Promise((resolve, reject) => process.stdout.write(frame, (error) => error ? reject(error) : resolve())),
  );
  return writeQueue;
}

function callCore(method, params) {
  const id = `h-${nextRequestId++}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`core did not answer ${id}`));
    }, 5000);
    pending.set(id, {
      resolve: (packet) => {
        clearTimeout(timer);
        if (packet.error) reject(new Error(packet.error.message ?? "core request failed"));
        else resolve(packet.result);
      },
    });
    void send({ jsonrpc: "2.0", id, method, params });
  });
}

async function dispatch(packet) {
  const { method, params = {} } = packet;
  if (method === "host.malformed") {
    writeQueue = writeQueue.then(
      () => new Promise((resolve, reject) => process.stdout.write("{not-json}\n", (error) => error ? reject(error) : resolve())),
    );
    await writeQueue;
    return;
  }
  if (method === "host.oversize") {
    const response = JSON.stringify({ jsonrpc: "2.0", id: packet.id, result: { padding: "x".repeat(1024 * 1024 + 32) } });
    writeQueue = writeQueue.then(
      () => new Promise((resolve, reject) => process.stdout.write(`${response}\n`, (error) => error ? reject(error) : resolve())),
    );
    await writeQueue;
    return;
  }
  if (method === "host.delay") {
    await new Promise((resolve) => setTimeout(resolve, Number(params.milliseconds ?? 0)));
    await send({ jsonrpc: "2.0", id: packet.id, result: { worker: "node", done: true } });
    return;
  }
  if (method === "host.echo") {
    const reverse = await callCore("core.reverse", { value: params.value });
    await send({
      jsonrpc: "2.0",
      id: packet.id,
      result: { worker: "node", echo: params.value, reverse },
    });
    return;
  }
  await send({ jsonrpc: "2.0", id: packet.id, error: { code: -32601, message: "method not found" } });
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let packet;
  try {
    packet = JSON.parse(line);
  } catch (error) {
    console.error(`bad inbound frame: ${error}`);
    process.exitCode = 2;
    break;
  }
  if (packet.jsonrpc !== "2.0") {
    console.error("bad JSON-RPC version");
    process.exitCode = 2;
    break;
  }
  if (typeof packet.method === "string") {
    if (Object.hasOwn(packet, "id")) void dispatch(packet).catch((error) => console.error(error));
  } else if (typeof packet.id === "string") {
    pending.get(packet.id)?.resolve(packet);
    pending.delete(packet.id);
  }
}
