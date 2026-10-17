import { Ajv2020 } from "ajv/dist/2020.js";
import { createRequire } from "node:module";
import { isDeepStrictEqual } from "node:util";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const contractDirectory = fileURLToPath(new URL("../../../contracts/v0.1/", import.meta.url));
const ajv = new Ajv2020({ allErrors: true, strict: true });
const addFormats = createRequire(import.meta.url)("ajv-formats") as (instance: Ajv2020) => Ajv2020;
addFormats(ajv);
const eventSchema = JSON.parse(
  readFileSync(`${contractDirectory}/event-envelope.schema.json`, "utf8"),
);
const operationSchema = JSON.parse(
  readFileSync(`${contractDirectory}/platform-operation.schema.json`, "utf8"),
);
const capabilitySchema = JSON.parse(
  readFileSync(`${contractDirectory}/capability-descriptor.schema.json`, "utf8"),
);
const validateEventValue = ajv.compile(eventSchema);
const validateOperationValue = ajv.compile(operationSchema);
const validateCapabilityDescriptorValue = ajv.compile(capabilitySchema);

export type LabDocumentKind = "catalog" | "run" | "result" | "summary";

const labContractDirectory = fileURLToPath(new URL("../../../contracts/lab/v1/", import.meta.url));
const labSchemas = new Map<LabDocumentKind, Record<string, unknown>>();
for (const kind of ["catalog", "result", "summary", "run"] as const) {
  const schema = JSON.parse(
    readFileSync(`${labContractDirectory}/${kind}.schema.json`, "utf8"),
  ) as Record<string, unknown>;
  labSchemas.set(kind, schema);
  ajv.addSchema(schema);
}
const labValidators = new Map<LabDocumentKind, NonNullable<ReturnType<typeof ajv.getSchema>>>();
for (const [kind, schema] of labSchemas) {
  const validate = ajv.getSchema(schema["$id"] as string);
  if (!validate) throw new Error(`lab schema was not registered: ${kind}`);
  labValidators.set(kind, validate);
}

export function validateLabDocument(kind: LabDocumentKind, value: unknown): string[] {
  const validate = labValidators.get(kind);
  if (!validate) return [`unknown lab document kind: ${kind}`];
  return validate(value)
    ? []
    : (validate.errors ?? []).map((error) => error.message ?? `invalid ${kind}`).sort();
}

export const supportedOperations = new Set(["send_message", "get_message", "get_self_info"]);

export function validateEvent(value: unknown): string[] {
  return validateEventValue(value)
    ? []
    : (validateEventValue.errors ?? []).map((error) => error.message ?? "invalid event").sort();
}

export function eventErrorCode(value: unknown): "invalid_request" | "protocol_mismatch" | undefined {
  if (value && typeof value === "object" && "schema_version" in value) {
    const version = (value as { schema_version?: unknown }).schema_version;
    if (typeof version === "string" && version !== "ligamina/0.1") return "protocol_mismatch";
  }
  return validateEvent(value).length ? "invalid_request" : undefined;
}

export function validateOperation(value: unknown): string[] {
  return validateOperationValue(value)
    ? []
    : (validateOperationValue.errors ?? []).map((error) => error.message ?? "invalid operation").sort();
}

export function validateCapabilityDescriptor(value: unknown): string[] {
  return validateCapabilityDescriptorValue(value)
    ? []
    : (validateCapabilityDescriptorValue.errors ?? []).map((error) => error.message ?? "invalid capability descriptor").sort();
}

export function classifyOperationReuse(
  existing: Record<string, unknown>,
  incoming: Record<string, unknown>,
): "new" | "idempotent" | "operation_conflict" {
  if (existing.operation_id !== incoming.operation_id) return "new";
  const payloadFields = ["account_id", "operation", "target", "params"] as const;
  return payloadFields.every((field) => isDeepStrictEqual(existing[field], incoming[field]))
    ? "idempotent"
    : "operation_conflict";
}

export function classifyOperationSupport(operation: string): "supported" | "unsupported_api" {
  return supportedOperations.has(operation) ? "supported" : "unsupported_api";
}
