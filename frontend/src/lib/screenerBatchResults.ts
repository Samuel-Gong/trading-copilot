export interface ScreenerBatchRowsResult {
  as_of: string
  total: number
  rows: any[]
}

export interface ScreenerBatchResultSource {
  as_of: string | null
  results: Record<string, ScreenerBatchRowsResult>
  ext_columns?: string
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

export function updateTransientBatchResult(
  source: ScreenerBatchResultSource | null,
  strategyId: string,
  result: ScreenerBatchRowsResult,
) {
  if (!source || source.as_of !== result.as_of) return source
  return {
    ...source,
    results: { ...source.results, [strategyId]: result },
  }
}

export function shouldRefreshTransientBatchForColumns(
  source: ScreenerBatchResultSource | null,
  asOf: string,
  extColumns?: string,
) {
  return source?.as_of === asOf && (source.ext_columns ?? '') !== (extColumns ?? '')
}
