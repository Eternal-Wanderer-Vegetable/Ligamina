/** Explicit pure capability declarations exposed by the Koishi Host. */

import { Ajv2020 } from 'ajv/dist/2020.js'

export const CAPABILITY_ID = 'ligamina.koishi.math.add'

const inputSchema = {
  type: 'object',
  properties: {
    left: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
    right: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
  },
  required: ['left', 'right'],
  additionalProperties: false,
} as const
const outputSchema = { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 } as const
const descriptors = [{
  capability_id: CAPABILITY_ID,
  input_schema: inputSchema,
  output_schema: outputSchema,
  pure: true,
}]
const ajv = new Ajv2020({ allErrors: true, strict: true })
const validateInput = ajv.compile(inputSchema)
const validateOutput = ajv.compile(outputSchema)

export class CapabilityError extends Error {
  constructor(public code: string, message: string) {
    super(message)
  }
}

export function capabilityDescriptors() {
  return structuredClone(descriptors)
}

export function invokeCapability(capabilityId: string, argumentsValue: unknown): number {
  if (capabilityId !== CAPABILITY_ID) {
    throw new CapabilityError('unsupported_capability', `unsupported capability: ${capabilityId}`)
  }
  if (!validateInput(argumentsValue)) {
    const errors = (validateInput.errors ?? []).map((error) => error.message ?? 'invalid arguments').sort()
    throw new CapabilityError('invalid_request', errors.join('; '))
  }
  const args = argumentsValue as { left: number; right: number }
  const result = args.left + args.right
  if (!validateOutput(result)) throw new CapabilityError('invalid_result', 'capability result does not match its declared schema')
  return result
}
