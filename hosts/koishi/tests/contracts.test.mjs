import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  classifyOperationReuse,
  classifyOperationSupport,
  eventErrorCode,
  validateEvent,
  validateCapabilityDescriptor,
  validateOperation,
} from "../dist/contracts.js";

const fixtureDirectory = fileURLToPath(new URL("../../../contracts/v0.1/fixtures/", import.meta.url));
const vectors = JSON.parse(readFileSync(`${fixtureDirectory}/vectors.json`, "utf8"));
const fixture = (name) => JSON.parse(readFileSync(`${fixtureDirectory}/${name}`, "utf8"));

test("all language-neutral event schema vectors agree", () => {
  for (const vector of vectors.event_schema) {
    const value = fixture(vector.file);
    assert.equal(validateEvent(value).length === 0, vector.valid, vector.name);
    assert.equal(eventErrorCode(value), vector.error_code ?? undefined, vector.name);
  }
});

test("all language-neutral operation and collision vectors agree", () => {
  for (const vector of vectors.operation_schema) {
    if (vector.file) {
      const value = fixture(vector.file);
      assert.equal(validateOperation(value).length === 0, vector.valid, vector.name);
      if (vector.outcome === "unsupported_api") {
        assert.equal(classifyOperationSupport(value.operation), "unsupported_api");
      }
    } else {
      const existing = fixture(vector.existing);
      const incoming = fixture(vector.incoming);
      assert.deepEqual(validateOperation(existing), []);
      assert.deepEqual(validateOperation(incoming), []);
      assert.equal(classifyOperationReuse(existing, incoming), vector.outcome, vector.name);
    }
  }

  assert.equal(classifyOperationReuse({ operation_id: "a" }, { operation_id: "b" }), "new");
});

test("all language-neutral capability descriptor vectors agree", () => {
  for (const vector of vectors.capability_descriptor) {
    const value = fixture(vector.file);
    assert.equal(validateCapabilityDescriptor(value).length === 0, vector.valid, vector.name);
  }
});
