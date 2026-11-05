// Registered tool results follow the host's string | content blocks | undefined
// contract. Serialize structured data at this boundary, never inside native tools.
export const answer = (value: unknown): { result: string } => ({ result: typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '' })
