/**
 * Bot-neutral OneBot v11 Gateway (TOOL-05).
 *
 * Connects the Core's platform-operation queue to any OneBot v11 HTTP implementation
 * (for example NapCat, Lagrange or LLOneBot), and converts OneBot message events into
 * Core event envelopes. Nothing here is specific to one bot: the bot (or the operator)
 * owns the OneBot connection and simply runs this Gateway next to it.
 *
 * Delivery rule: a send whose outcome is uncertain (network error, timeout, async
 * acceptance) is reported as `unknown` and never retried, matching the Core ledger.
 */

import type { EventEnvelope, GatewayLease, LigaminaClient } from "./client.js";

const PROTOCOL_VERSION = "ligamina/0.1";
const ONEBOT_PLATFORM = "onebot.v11";

export interface OneBotEndpoint {
  /** Base URL of the OneBot v11 HTTP API, e.g. `http://127.0.0.1:3000`. */
  baseUrl: string;
  /** Optional OneBot access token, sent as a bearer token. */
  accessToken?: string;
}

export interface OneBotGatewayOptions {
  core: LigaminaClient;
  gatewayId: string;
  registrationToken: string;
  onebot: OneBotEndpoint;
  leaseTtlMs?: number;
  /** Per-action timeout for OneBot HTTP calls. */
  actionTimeoutMs?: number;
  fetch?: typeof fetch;
  now?: () => number;
}

interface ClaimedOperation {
  key: { client_id: string; account_id: string; operation: string; operation_id: string };
  fingerprint: string;
  operation_payload: {
    account_id: string;
    operation: string;
    target: { kind: "group" | "private"; id: string };
    params: Record<string, unknown>;
  };
}

interface Outcome {
  status: "succeeded" | "failed" | "unknown";
  platform_message_id?: string;
  data?: unknown;
  error?: string;
}

type Segment = Record<string, unknown>;

export class OneBotGateway {
  readonly #options: Required<Omit<OneBotGatewayOptions, "fetch" | "now">> & {
    fetch: typeof fetch;
    now: () => number;
  };
  #lease?: GatewayLease;

  constructor(options: OneBotGatewayOptions) {
    this.#options = {
      leaseTtlMs: 60_000,
      actionTimeoutMs: 15_000,
      fetch: options.fetch ?? fetch,
      now: options.now ?? Date.now,
      ...options,
    } as never;
  }

  /** Claims and performs at most one action. Returns false when nothing was pending. */
  async pollOnce(): Promise<boolean> {
    const lease = await this.#currentLease();
    const operation = (await this.#options.core.claimGatewayAction<ClaimedOperation>(
      this.#options.gatewayId,
      lease,
    )) as ClaimedOperation | null;
    if (!operation) return false;
    const outcome = await this.#perform(operation.operation_payload);
    await this.#options.core.reportGatewayResult(this.#options.gatewayId, operation.key.operation_id, lease, {
      key: operation.key,
      fingerprint: operation.fingerprint,
      status: outcome.status,
      platform_message_id: outcome.platform_message_id ?? null,
      data: outcome.data ?? null,
      error: outcome.error ?? null,
      observed_at_ms: this.#options.now(),
      evidence_ref: outcome.platform_message_id ? `onebot.v11://message/${outcome.platform_message_id}` : null,
    });
    return true;
  }

  /** Polls until the signal aborts, idling between empty claims. */
  async run(signal: AbortSignal, idleMs = 200): Promise<void> {
    while (!signal.aborted) {
      const handled = await this.pollOnce();
      if (!handled) await new Promise((resolve) => setTimeout(resolve, idleMs));
    }
  }

  async #currentLease(): Promise<GatewayLease> {
    const lease = this.#lease;
    const margin = this.#options.leaseTtlMs / 3;
    if (!lease || lease.expires_at_ms - this.#options.now() < margin) {
      this.#lease = await this.#options.core.acquireGatewayLease<GatewayLease>(
        this.#options.gatewayId,
        this.#options.registrationToken,
        this.#options.leaseTtlMs,
      );
    }
    return this.#lease!;
  }

  async #perform(payload: ClaimedOperation["operation_payload"]): Promise<Outcome> {
    switch (payload.operation) {
      case "send_message": {
        const message = toOneBotMessage(payload.params.message);
        const action = payload.target.kind === "group" ? "send_group_msg" : "send_private_msg";
        const target = payload.target.kind === "group"
          ? { group_id: oneBotId(payload.target.id) }
          : { user_id: oneBotId(payload.target.id) };
        const response = await this.#call(action, { ...target, message });
        if (response.status !== "succeeded") return response;
        const id = (response.data as { message_id?: unknown } | undefined)?.message_id;
        if (id === undefined || id === null) {
          return { status: "unknown", error: "OneBot accepted the send but returned no message_id" };
        }
        return { status: "succeeded", platform_message_id: String(id) };
      }
      case "get_message": {
        const response = await this.#call("get_msg", { message_id: oneBotId(String(payload.params.message_id)) });
        if (response.status !== "succeeded") return response;
        const data = response.data as Record<string, any>;
        return {
          status: "succeeded",
          data: {
            target: payload.target,
            message_id: String(data.message_id),
            real_id: String(data.real_id ?? data.message_id),
            actor_id: String(data.sender?.user_id ?? ""),
            nickname: data.sender?.nickname,
            timestamp: new Date(Number(data.time) * 1000).toISOString(),
            message: fromOneBotMessage(data.message),
          },
        };
      }
      case "get_self_info": {
        const response = await this.#call("get_login_info", {});
        if (response.status !== "succeeded") return response;
        const data = response.data as { user_id?: unknown; nickname?: unknown };
        return { status: "succeeded", data: { user_id: String(data.user_id), nickname: String(data.nickname ?? "") } };
      }
      default:
        return { status: "failed", error: `unsupported operation ${payload.operation}` };
    }
  }

  /** One OneBot HTTP action. Any doubt about whether it took effect yields `unknown`. */
  async #call(action: string, body: Record<string, unknown>): Promise<Outcome> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.#options.onebot.accessToken) headers.authorization = `Bearer ${this.#options.onebot.accessToken}`;
    let response: Response;
    try {
      response = await this.#options.fetch(`${this.#options.onebot.baseUrl.replace(/\/+$/, "")}/${action}`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(this.#options.actionTimeoutMs),
      });
    } catch (error) {
      return { status: "unknown", error: `OneBot ${action} outcome unknown: ${(error as Error).name}` };
    }
    let payload: { status?: string; retcode?: number; data?: unknown; message?: string; wording?: string };
    try {
      payload = await response.json();
    } catch {
      return { status: "unknown", error: `OneBot ${action} returned HTTP ${response.status} without JSON` };
    }
    if (payload.retcode === 0 && payload.status === "ok") return { status: "succeeded", data: payload.data };
    // retcode 1 is OneBot's "accepted asynchronously": delivery is not confirmed.
    if (payload.retcode === 1) return { status: "unknown", error: `OneBot ${action} was accepted asynchronously` };
    return {
      status: "failed",
      error: `OneBot ${action} failed with retcode ${String(payload.retcode)}: ${payload.wording ?? payload.message ?? ""}`.trim(),
    };
  }
}

/** Converts a OneBot v11 message event into a Core event envelope for `targetHost`. */
export function toEventEnvelope(event: Record<string, any>, targetHost: string): EventEnvelope | null {
  if (event.post_type !== "message" || (event.message_type !== "group" && event.message_type !== "private")) return null;
  const accountId = String(event.self_id);
  const conversation = event.message_type === "group"
    ? { kind: "group" as const, id: String(event.group_id) }
    : { kind: "private" as const, id: String(event.user_id) };
  const messageId = String(event.message_id);
  const sender = event.sender && typeof event.sender === "object" ? event.sender : {};
  return {
    schema_version: PROTOCOL_VERSION as EventEnvelope["schema_version"],
    event_id: `onebot-${accountId}-${messageId}`,
    trace_id: `trace-onebot-${accountId}-${messageId}`,
    platform: ONEBOT_PLATFORM,
    account_id: accountId,
    conversation,
    actor_id: String(event.user_id),
    timestamp: new Date(Number(event.time) * 1000).toISOString(),
    target_host: targetHost,
    message: fromOneBotMessage(event.message),
    extensions: {
      [ONEBOT_PLATFORM]: {
        message_id: messageId,
        ...(typeof sender.nickname === "string" ? { sender: { nickname: sender.nickname } } : {}),
      },
    },
  };
}

export function toOneBotMessage(message: unknown): Segment[] {
  if (!Array.isArray(message)) throw new TypeError("send_message params.message must be a segment array");
  return message.map((segment: Record<string, unknown>) => {
    switch (segment.type) {
      case "text": return { type: "text", data: { text: String(segment.text ?? "") } };
      case "mention": return { type: "at", data: { qq: String(segment.user_id) } };
      case "reply": return { type: "reply", data: { id: String(segment.message_id) } };
      case "image_ref": return { type: "image", data: { file: String(segment.resource_ref) } };
      default: throw new TypeError(`unsupported message segment ${String(segment.type)}`);
    }
  });
}

export function fromOneBotMessage(message: unknown): Segment[] {
  if (typeof message === "string") return message ? [{ type: "text", text: message }] : [];
  if (!Array.isArray(message)) return [];
  const segments: Segment[] = [];
  for (const segment of message as Array<{ type?: string; data?: Record<string, unknown> }>) {
    const data = segment.data ?? {};
    if (segment.type === "text") segments.push({ type: "text", text: String(data.text ?? "") });
    else if (segment.type === "at") segments.push({ type: "mention", user_id: String(data.qq) });
    else if (segment.type === "reply") segments.push({ type: "reply", message_id: String(data.id) });
    else if (segment.type === "image") segments.push({ type: "image_ref", resource_ref: String(data.file ?? data.url ?? "") });
  }
  return segments;
}

/** OneBot implementations expect numeric IDs; keep strings that exceed the safe range. */
function oneBotId(id: string): number | string {
  return /^\d+$/.test(id) && Number.isSafeInteger(Number(id)) ? Number(id) : id;
}
