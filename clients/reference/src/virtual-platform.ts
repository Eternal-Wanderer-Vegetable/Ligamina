export type MessageSegment = Record<string, unknown>;
export type ConversationTarget = { kind: "group" | "private"; id: string };

export interface SendMessageOperation {
  operation_id: string;
  trace_id: string;
  account_id: string;
  operation: "send_message";
  target: ConversationTarget;
  params: { message: MessageSegment[] };
}

export interface VirtualMessage {
  message_id: string;
  account_id: string;
  target: ConversationTarget;
  message: MessageSegment[];
  operation_id: string;
}

export interface VirtualSendResult {
  status: "succeeded";
  platform_message_id: string;
  observed_at_ms: number;
  evidence_ref: string;
}

export class OperationConflictError extends Error {
  constructor() {
    super("operation ID was reused with a different payload");
    this.name = "OperationConflictError";
  }
}

/** In-memory platform owned by a reference customer gateway and isolated per account. */
export class VirtualPlatform {
  readonly #messages = new Map<string, VirtualMessage[]>();
  readonly #operations = new Map<string, { fingerprint: string; result: VirtualSendResult }>();
  #nextMessageId = 1;

  send(operation: SendMessageOperation): VirtualSendResult {
    if (operation.operation !== "send_message") {
      throw new TypeError(`unsupported virtual platform operation: ${operation.operation}`);
    }
    const operationKey = `${operation.account_id}\u0000${operation.operation}\u0000${operation.operation_id}`;
    const fingerprint = canonicalJson({
      account_id: operation.account_id,
      operation: operation.operation,
      target: operation.target,
      params: operation.params,
    });
    const prior = this.#operations.get(operationKey);
    if (prior) {
      if (prior.fingerprint !== fingerprint) throw new OperationConflictError();
      return { ...prior.result };
    }

    const messageId = `virtual-${this.#nextMessageId++}`;
    const message: VirtualMessage = {
      message_id: messageId,
      account_id: operation.account_id,
      target: structuredClone(operation.target),
      message: structuredClone(operation.params.message),
      operation_id: operation.operation_id,
    };
    const targetKey = this.#targetKey(operation.account_id, operation.target);
    const history = this.#messages.get(targetKey) ?? [];
    history.push(message);
    this.#messages.set(targetKey, history);
    const result: VirtualSendResult = {
      status: "succeeded",
      platform_message_id: messageId,
      observed_at_ms: Date.now(),
      evidence_ref: `virtual-platform/${messageId}`,
    };
    this.#operations.set(operationKey, { fingerprint, result });
    return { ...result };
  }

  getMessage(
    accountId: string,
    target: ConversationTarget,
    messageId: string,
  ): VirtualMessage | undefined {
    const message = this.#messages
      .get(this.#targetKey(accountId, target))
      ?.find((candidate) => candidate.message_id === messageId);
    return message ? structuredClone(message) : undefined;
  }

  history(accountId: string, target: ConversationTarget, limit = 50): VirtualMessage[] {
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError("history limit must be between 1 and 500");
    }
    const messages = this.#messages.get(this.#targetKey(accountId, target)) ?? [];
    return structuredClone(messages.slice(-limit));
  }

  #targetKey(accountId: string, target: ConversationTarget): string {
    return `${accountId}\u0000${target.kind}\u0000${target.id}`;
  }
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a.localeCompare(b),
    );
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
