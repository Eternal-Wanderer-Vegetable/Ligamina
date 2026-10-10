import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import {
  CallToolResultSchema,
  type ListToolsResult,
} from "@modelcontextprotocol/sdk/types.js";

/** Independent MCP SDK client for Ligamina's 2025-11-25 endpoint. */
export class LigaminaMcpClient {
  readonly #client: Client;

  constructor(name = "ligamina-reference-client", version = "0.1.0") {
    this.#client = new Client({ name, version }, { capabilities: {} });
  }

  connect(transport: Transport): Promise<void> {
    return this.#client.connect(transport);
  }

  listTools(): Promise<ListToolsResult> {
    return this.#client.listTools();
  }

  callTool(
    name: string,
    args: Record<string, unknown> = {},
  ): ReturnType<Client["callTool"]> {
    return this.#client.callTool({ name, arguments: args }, CallToolResultSchema);
  }

  close(): Promise<void> {
    return this.#client.close();
  }
}
