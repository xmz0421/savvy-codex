// Test boundary for the native engine's mock transport. Unlike runtime, the
// testing kit can return unchecked values from a fixture. Observe raw values
// before JSON decoding so fixtures cannot hide a production wire regression.
type Dict = Record<string, unknown>
const record = (v: unknown): v is Dict => !!v && typeof v === 'object' && !Array.isArray(v)
const blocks = (v: unknown): boolean => Array.isArray(v) && v.every(block => record(block) && (
  block.type === 'text' ? typeof block.text === 'string' :
  block.type === 'image' ? record(block.source) && (block.source.type === 'base64' ? typeof block.source.data === 'string' && typeof block.source.media_type === 'string' : block.source.type === 'url' && typeof block.source.url === 'string') :
  block.type === 'resource' ? record(block.resource) && typeof block.resource.uri === 'string' && (typeof block.resource.text === 'string' || typeof block.resource.blob === 'string') :
  block.type === 'resource_link' ? typeof block.uri === 'string' && typeof block.name === 'string' : false))
export function assertHostToolResult(tool: string, raw: unknown): void {
  if (!record(raw)) throw new Error('Host output shape: tool response must be an object')
  if (raw.deny !== undefined) {
    if (typeof raw.deny !== 'string') throw new Error('Host output shape: deny must be a string')
    return
  }
  const value = raw.result
  if (tool.startsWith('mcp__')) {
    if (value !== undefined && typeof value !== 'string' && !blocks(value)) throw new Error('Host output shape: expected string, valid content blocks, or undefined')
  } else if (tool === 'Agent') {
    if (!record(value) || !['completed', 'async_launched', 'remote_launched'].includes(String(value.status)) || typeof value.agentId !== 'string') throw new Error('Host Agent output shape: agentId/status required')
    if (value.status === 'completed' && (!blocks(value.content) || !['totalToolUseCount', 'totalDurationMs', 'totalTokens'].every(key => typeof value[key] === 'number'))) throw new Error('Host Agent output shape: completed content and totals required')
  } else if (tool === 'Read') {
    if (!record(value) || value.type !== 'text' || !record(value.file) || typeof value.file.filePath !== 'string' || typeof value.file.content !== 'string' || !['numLines', 'startLine', 'totalLines'].every(key => typeof (value.file as Dict)[key] === 'number')) throw new Error('Host Read output shape: expected native text file fields')
  } else if (tool === 'Write') {
    if (!record(value) || !['create', 'update'].includes(String(value.type)) || typeof value.filePath !== 'string' || typeof value.content !== 'string' || !Array.isArray(value.structuredPatch) || !(value.originalFile === null || typeof value.originalFile === 'string')) throw new Error('Host Write output shape: expected native file fields')
  }
}

export const nativeReadFixture = (filePath: string, content: string) => ({ result: { type: 'text', file: { filePath, content, numLines: content.split('\n').length, startLine: 1, totalLines: content.split('\n').length } } })
export const nativeWriteFixture = (filePath: string, content: string) => ({ result: { type: 'create', filePath, content, structuredPatch: [], originalFile: null } })
