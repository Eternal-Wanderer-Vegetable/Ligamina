export interface EventEnvelope {
  schema_version: "ligamina/0.1";
  event_id: string;
  trace_id: string;
  platform: string;
  account_id: string;
  conversation: { kind: "group" | "private"; id: string };
  actor_id: string;
  timestamp: string;
  target_host: string;
  message: Array<Record<string, unknown>>;
  extensions?: Record<string, unknown>;
}

export interface GatewayLease {
  lease_id: string;
  token: string;
  lease_epoch: number;
  core_epoch: string;
  expires_at_ms: number;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ClientOptions {
  baseUrl: string;
  token: string;
  fetch?: typeof fetch;
}

/** Small typed client for local management, event, operation, and gateway modes. */
export class LigaminaClient {
  readonly #baseUrl: string;
  readonly #token: string;
  readonly #fetch: typeof fetch;

  constructor(options: ClientOptions) {
    if (!options.token) throw new TypeError("an access token is required");
    this.#baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.#token = options.token;
    this.#fetch = options.fetch ?? fetch;
  }

  status<T = unknown>(): Promise<T> {
    return this.#request<T>("/v1/status");
  }

  setHostDesired<T = unknown>(hostId: string, desired: unknown): Promise<T> {
    return this.#request<T>(`/v1/hosts/${encodeURIComponent(hostId)}/desired`, {
      method: "PUT",
      body: JSON.stringify(desired),
    });
  }

  submitEvent<T = unknown>(event: EventEnvelope): Promise<T> {
    return this.#request<T>("/v1/events", {
      method: "POST",
      body: JSON.stringify(event),
    });
  }

  eventStatus<T = unknown>(eventId: string): Promise<T> {
    return this.#request<T>(`/v1/events/${encodeURIComponent(eventId)}`);
  }

  acquireGatewayLease<T = GatewayLease>(
    gatewayId: string,
    registrationToken: string,
    ttlMs: number,
  ): Promise<T> {
    return this.#request<T>(`/v1/gateways/${encodeURIComponent(gatewayId)}/lease`, {
      method: "POST",
      token: registrationToken,
      body: JSON.stringify({ ttl_ms: ttlMs }),
    });
  }

  claimGatewayAction<T = unknown>(gatewayId: string, leaseToken: string): Promise<T> {
    return this.#request<T>(`/v1/gateways/${encodeURIComponent(gatewayId)}/actions/claim`, {
      method: "POST",
      token: leaseToken,
      body: "{}",
    });
  }

  reportGatewayResult<T = unknown>(
    gatewayId: string,
    operationId: string,
    leaseToken: string,
    result: unknown,
  ): Promise<T> {
    return this.#request<T>(
      `/v1/gateways/${encodeURIComponent(gatewayId)}/actions/${encodeURIComponent(operationId)}/result`,
      { method: "POST", token: leaseToken, body: JSON.stringify(result) },
    );
  }

  operationStatus<T = unknown>(operationId: string): Promise<T> {
    return this.#request<T>(`/v1/operations/${encodeURIComponent(operationId)}`);
  }

  async #request<T>(
    path: string,
    options: { method?: string; body?: string; token?: string } = {},
  ): Promise<T> {
    const headers = new Headers({
      authorization: `Bearer ${options.token ?? this.#token}`,
      accept: "application/json",
    });
    if (options.body !== undefined) headers.set("content-type", "application/json");
    const response = await this.#fetch(`${this.#baseUrl}${path}`, {
      method: options.method ?? "GET",
      headers,
      ...(options.body === undefined ? {} : { body: options.body }),
    });
    const payload = response.status === 204 ? undefined : await response.json();
    if (!response.ok) {
      const error = (payload ?? {}) as { error?: { code?: string; message?: string } };
      throw new ApiError(
        response.status,
        error.error?.code ?? "http_error",
        error.error?.message ?? `Ligamina returned HTTP ${response.status}`,
      );
    }
    return payload as T;
  }
}
