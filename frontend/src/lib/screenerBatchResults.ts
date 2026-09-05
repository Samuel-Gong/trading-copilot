export interface ScreenerBatchRowsResult {
  as_of: string
  total: number
  rows: any[]
}

export interface ScreenerBatchResultSource {
  as_of: string | null
  results: Record<string, ScreenerBatchRowsResult>
}

export function requiresTransientBatchRows(requestedAsOf?: string, cachedAsOf?: string | null) {
  return !!requestedAsOf && !!cachedAsOf && requestedAsOf < cachedAsOf
}

export function resultsForSelectedDate(
  asOf: string,
  transient: ScreenerBatchResultSource | null,
  cached: ScreenerBatchResultSource | undefined,
) {
  const source = transient?.as_of === asOf
    ? transient
    : cached?.as_of === asOf
      ? cached
      : null
  if (!source) return null
  return Object.fromEntries(
    Object.entries(source.results).filter(([, result]) => result.as_of === asOf),
  )
}
