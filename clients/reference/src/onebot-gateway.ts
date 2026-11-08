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
 *
 * Fault handling (P6-02). Each claimed operation goes through three stages, and an
 * exception in any of them is contained to that operation:
 *
 * 1. prepare  - validation and segment conversion. Nothing was sent: `failed`.
 * 2. platform - the OneBot call. Transport doubt is `unknown` (see `#call`); an
 *    exception while interpreting the reply is `unknown` for sends and `failed` for
 *    read-only actions.
 * 3. report   - writing the outcome to Core. A transient failure keeps the outcome and
 *    re-reports it under the same lease (Core accepts an identical result twice); the
 *    platform action is never repeated. When the lease has expired or Core rejects the
 *    report, the outcome is handed to `onUnreported` and dropped: Core has recorded (or
 *    will record) the operation as `unknown`, which must not be replayed.
 *
 * `run()` survives Core restarts and OneBot outages: errors back off, and a lease that
 * Core refuses is discarded so the next poll acquires a fresh one.
 */

import { ApiError, type EventEnvelope, type GatewayLease, type LigaminaClient } from "./client.js";

const PROTOCOL_VERSION = "ligamina/0.1";
const ONEBOT_PLATFORM = "onebot.v11";
const MAX_BACKOFF_MS = 5_000;

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
  /** Receives errors that `run()` recovers from (Core or OneBot unavailable, ...). */
  onError?: (error: unknown) => void;
  /**
   * Receives an outcome that could not be written to Core and was dropped. The Core
   * ledger keeps that operation `unknown`; operators reconcile it from this record.
   */
  onUnreported?: (record: UnreportedOutcome) => void;
}

export interface UnreportedOutcome {
  operation_id: string;
  key: ClaimedOperation["key"];
  status: Outcome["status"];
  platform_message_id: string | null;
  reason: string;
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

/** A prepared OneBot action plus how to read its reply. */
interface PlatformAction {
  action: string;
  body: Record<string, unknown>;
  /** True when the action may change platform state (a send). */
  sideEffect: boolean;
  interpret: (data: unknown) => Outcome;
}

interface PendingReport {
  operation: ClaimedOperation;
  lease: GatewayLease;
  outcome: Outcome;
  result: Record<string, unknown>;
}

type ResolvedOptions = Required<Omit<OneBotGatewayOptions, "fetch" | "now" | "onError" | "onUnreported">> & {
  fetch: typeof fetch;
  now: () => number;
  onError: (error: unknown) => void;
  onUnreported: (record: UnreportedOutcome) => void;
};

export class OneBotGateway {
  readonly #options: ResolvedOptions;
  #lease?: GatewayLease;
  #pending?: PendingReport;

  constructor(options: OneBotGatewayOptions) {
    this.#options = {
      leaseTtlMs: 60_000,
      actionTimeoutMs: 15_000,
      ...options,
      fetch: options.fetch ?? fetch,
      now: options.now ?? Date.now,
      onError: options.onError ?? ((error) => console.error("[OneBotGateway]", error)),
      onUnreported: options.onUnreported
        ?? ((record) => console.error("[OneBotGateway] outcome not reported to Core", record)),
    };
  }

  /** True while an outcome is waiting to be written to Core. */
  get hasPendingReport(): boolean {
    return this.#pending !== undefined;
  }

  /**
   * Claims and performs at most one action. Returns false when nothing was pending.
   *
   * An outcome left unreported by an earlier poll is flushed first, and no new action is
   * claimed until it has been written or dropped. Core errors (lease, claim, report)
   * propagate; an operation's own preparation or platform errors never do.
   */
  async pollOnce(): Promise<boolean> {
    if (this.#pending) {
      await this.#flushReport();
      return true;
    }
    const lease = await this.#currentLease();
    let operation: ClaimedOperation | null;
    try {
      operation = (await this.#options.core.claimGatewayAction<ClaimedOperation>(
        this.#options.gatewayId,
        lease,
      )) as ClaimedOperation | null;
    } catch (error) {
      this.#forgetLeaseOnDenial(error);
      throw error;
    }
    if (!operation) return false;
    const outcome = await this.#perform(operation.operation_payload);
    this.#pending = {
      operation,
      lease,
      outcome,
      result: {
        key: operation.key,
        fingerprint: operation.fingerprint,
        status: outcome.status,
        platform_message_id: outcome.platform_message_id ?? null,
        data: outcome.data ?? null,
        error: outcome.error ?? null,
        observed_at_ms: this.#options.now(),
        evidence_ref: outcome.platform_message_id ? `onebot.v11://message/${outcome.platform_message_id}` : null,
      },
    };
    await this.#flushReport();
    return true;
  }

  /**
   * Polls until the signal aborts, idling between empty claims. Errors go to `onError`
   * and are retried with capped exponential backoff; only an abort ends the loop.
   */
  async run(signal: AbortSignal, idleMs = 200): Promise<void> {
    let backoff = idleMs;
    while (!signal.aborted) {
      try {
        const handled = await this.pollOnce();
        backoff = idleMs;
        if (!handled) await sleep(idleMs, signal);
      } catch (error) {
        if (signal.aborted) break;
        this.#options.onError(error);
        await sleep(backoff, signal);
        backoff = Math.min(Math.max(backoff, 1) * 2, MAX_BACKOFF_MS);
      }
    }
  }

  /** Writes the pending outcome to Core; a transient failure keeps it for the next poll. */
  async #flushReport(): Promise<void> {
    const pending = this.#pending!;
    if (pending.lease.expires_at_ms <= this.#options.now()) {
      // Core reaps the lease and marks the operation unknown; this report cannot land.
      this.#drop(pending, "lease expired before the outcome was reported");
      return;
    }
    try {
      await this.#options.core.reportGatewayResult(
        this.#options.gatewayId,
        pending.operation.key.operation_id,
        pending.lease,
        pending.result,
      );
      this.#pending = undefined;
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        // Stale lease, conflicting result or malformed report: Core will not accept it.
        this.#forgetLeaseOnDenial(error);
        this.#drop(pending, `Core rejected the report: ${error.code}: ${error.message}`);
        return;
      }
      throw error;
    }
  }

  #drop(pending: PendingReport, reason: string): void {
    this.#pending = undefined;
    this.#options.onUnreported({
      operation_id: pending.operation.key.operation_id,
      key: pending.operation.key,
      status: pending.outcome.status,
      platform_message_id: pending.outcome.platform_message_id ?? null,
      reason,
    });
  }

  /** A lease Core refuses (expired, replaced, Core restarted) is useless; re-acquire next time. */
  #forgetLeaseOnDenial(error: unknown): void {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.#lease = undefined;
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

  /** Runs one operation through the prepare and platform stages. Never throws. */
  async #perform(payload: ClaimedOperation["operation_payload"]): Promise<Outcome> {
    let prepared: PlatformAction;
    try {
      prepared = prepareAction(payload);
    } catch (error) {
      return { status: "failed", error: `not sent: ${errorText(error)}` };
    }
    const response = await this.#call(prepared.action, prepared.body);
    if (response.status !== "succeeded") return response;
    try {
      return prepared.interpret(response.data);
    } catch (error) {
      const error_ = `OneBot ${prepared.action} reply could not be interpreted: ${errorText(error)}`;
      return { status: prepared.sideEffect ? "unknown" : "failed", error: error_ };
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
    if (!payload || typeof payload !== "object") {
      return { status: "unknown", error: `OneBot ${action} returned HTTP ${response.status} with a non-object body` };
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

/** Validates an operation and builds its OneBot action. Throws when it cannot be sent. */
function prepareAction(payload: ClaimedOperation["operation_payload"]): PlatformAction {
  if (!payload || typeof payload !== "object") throw new TypeError("operation payload is missing");
  const target = payload.target;
  const params = payload.params ?? {};
  switch (payload.operation) {
    case "send_message": {
      if (!target || (target.kind !== "group" && target.kind !== "private") || !target.id) {
        throw new TypeError("send_message requires a group or private target");
      }
      const message = toOneBotMessage(params.message);
      if (message.length === 0) throw new TypeError("send_message params.message is empty");
      return {
        action: target.kind === "group" ? "send_group_msg" : "send_private_msg",
        body: {
          ...(target.kind === "group" ? { group_id: oneBotId(target.id) } : { user_id: oneBotId(target.id) }),
          message,
        },
        sideEffect: true,
        interpret: (data) => {
          const id = (data as { message_id?: unknown } | null | undefined)?.message_id;
          if (id === undefined || id === null) {
            return { status: "unknown", error: "OneBot accepted the send but returned no message_id" };
          }
          return { status: "succeeded", platform_message_id: String(id) };
        },
      };
    }
    case "get_message": {
      if (params.message_id === undefined || params.message_id === null) {
        throw new TypeError("get_message requires params.message_id");
      }
      return {
        action: "get_msg",
        body: { message_id: oneBotId(String(params.message_id)) },
        sideEffect: false,
        interpret: (raw) => {
          if (!raw || typeof raw !== "object") throw new TypeError("get_msg returned no message");
          const data = raw as Record<string, any>;
          return {
            status: "succeeded",
            data: {
              target,
              message_id: String(data.message_id),
              real_id: String(data.real_id ?? data.message_id),
              actor_id: String(data.sender?.user_id ?? ""),
              nickname: data.sender?.nickname,
              timestamp: new Date(Number(data.time) * 1000).toISOString(),
              message: fromOneBotMessage(data.message),
            },
          };
        },
      };
    }
    case "get_self_info":
      return {
        action: "get_login_info",
        body: {},
        sideEffect: false,
        interpret: (raw) => {
          if (!raw || typeof raw !== "object") throw new TypeError("get_login_info returned no data");
          const data = raw as { user_id?: unknown; nickname?: unknown };
          return { status: "succeeded", data: { user_id: String(data.user_id), nickname: String(data.nickname ?? "") } };
        },
      };
    default:
      throw new TypeError(`unsupported operation ${String(payload.operation)}`);
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
    if (!segment || typeof segment !== "object") throw new TypeError("message segment must be an object");
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

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}
