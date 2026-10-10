import { readFile } from "node:fs/promises";
import { LigaminaClient } from "./client.js";

function usage(): string {
  return [
    "Usage: ligamina-reference <status|host-set|event|event-status|gateway-lease|gateway-claim|gateway-result|operation-status> [args]",
    "Authentication is read from LIGAMINA_TOKEN and the gateway-specific token environment variables.",
    "JSON request bodies are read from files; credentials are not accepted as command-line arguments.",
  ].join("\n");
}

async function readJson(path: string): Promise<unknown> {
  return JSON.parse(await readFile(path, "utf8")) as unknown;
}

async function run(args: string[], env = process.env): Promise<unknown> {
  const [mode, ...values] = args;
  const baseUrl = env.LIGAMINA_URL ?? "http://127.0.0.1:8765";
  const token = env.LIGAMINA_TOKEN;
  if (!mode || !token) throw new Error(usage());
  const client = new LigaminaClient({ baseUrl, token });
  switch (mode) {
    case "status":
      return client.status();
    case "host-set":
      if (values.length !== 2) throw new Error(usage());
      return client.setHostDesired(values[0]!, await readJson(values[1]!));
    case "event":
      if (values.length !== 1) throw new Error(usage());
      return client.submitEvent((await readJson(values[0]!)) as never);
    case "event-status":
      if (values.length !== 1) throw new Error(usage());
      return client.eventStatus(values[0]!);
    case "gateway-lease": {
      const registrationToken = env.LIGAMINA_GATEWAY_REGISTRATION_TOKEN;
      if (values.length !== 1 || !registrationToken) throw new Error(usage());
      return client.acquireGatewayLease(values[0]!, registrationToken, 60_000);
    }
    case "gateway-claim": {
      const leaseToken = env.LIGAMINA_GATEWAY_LEASE_TOKEN;
      if (values.length !== 1 || !leaseToken) throw new Error(usage());
      return client.claimGatewayAction(values[0]!, leaseToken);
    }
    case "gateway-result": {
      const leaseToken = env.LIGAMINA_GATEWAY_LEASE_TOKEN;
      if (values.length !== 3 || !leaseToken) throw new Error(usage());
      return client.reportGatewayResult(
        values[0]!,
        values[1]!,
        leaseToken,
        await readJson(values[2]!),
      );
    }
    case "operation-status":
      if (values.length !== 1) throw new Error(usage());
      return client.operationStatus(values[0]!);
    default:
      throw new Error(usage());
  }
}

if (process.argv[1]?.endsWith("cli.js")) {
  run(process.argv.slice(2))
    .then((result) => process.stdout.write(`${JSON.stringify(result, null, 2)}\n`))
    .catch((error: unknown) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 1;
    });
}

export { run as runCli };
