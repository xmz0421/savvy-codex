import { expect, test } from 'claude-code/testing'
import { assertHostToolResult, nativeReadFixture, nativeWriteFixture } from './host-contract'

// Native test fixtures are unchecked on 2.1.314. Strict observation supplies
// the same public wire contract even on hosts whose test transport omits it.
test('strict observer rejects a registered-tool object before JSON decoding', async ($, on) => {
  on('tool.call', { tool: 'mcp__wire_fixture__object' }, () => ({ result: { status: 'blocked' } }) as never)
  const raw = await $.tool.call({ tool: 'mcp__wire_fixture__object' } as never)
  expect(() => assertHostToolResult('mcp__wire_fixture__object', raw)).toThrow(/output shape/)
})

test('strict observer rejects a pretend Agent result', async ($, on) => {
  on('tool.call', { tool: 'Agent' }, () => ({ result: { status: 'blocked', summary: 'invalid native result' } }) as never)
  const raw = await $.tool.call({ tool: 'Agent', subagent_type: 'general-purpose', prompt: 'fixture only' } as never)
  expect(() => assertHostToolResult('Agent', raw)).toThrow(/output shape/)
})

test('native Agent gate uses deny without invoking a worker', async ($, on) => {
  let calls = 0
  on('tool.call', { tool: 'Agent' }, () => { calls++; throw new Error('worker must not run') })
  const raw = await $.tool.call({ tool: 'Agent', subagent_type: 'savvy-codex:savvy-review', prompt: 'fixture only' } as never)
  expect(raw).toMatchObject({ deny: expect.stringContaining('manual') })
  expect(calls).toBe(0)
  assertHostToolResult('Agent', raw)
})

test('observer accepts native text fixtures and valid registered blocks but rejects fake blocks', () => {
  assertHostToolResult('Read', nativeReadFixture('/repo/a.ts', 'real fixture text'))
  assertHostToolResult('Write', nativeWriteFixture('/repo/a.ts', 'real fixture text'))
  assertHostToolResult('mcp__fixture__text', { result: [{ type: 'text', text: 'ok' }] })
  assertHostToolResult('mcp__fixture__void', { result: undefined })
  expect(() => assertHostToolResult('mcp__fixture__bad', { result: [{ type: 'text' }] })).toThrow()
  expect(() => assertHostToolResult('Read', { result: 'ok' })).toThrow()
})
