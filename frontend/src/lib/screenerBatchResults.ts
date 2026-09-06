export interface ScreenerBatchRowsResult {
  as_of: string
  total: number
  rows: any[]
}

export interface ScreenerBatchResultSource {
  as_of: string | null
  results: Record<string, ScreenerBatchRowsResult>
  asset_type?: 'stock' | 'etf'
  ext_columns?: string
}

export function requiresTransientBatchRows(requestedAsOf?: string, cachedAsOf?: string | null) {
  return !!requestedAsOf && !!cachedAsOf && requestedAsOf < cachedAsOf
}

export function resultsForSelectedDate(
  asOf: string,
  transient: ScreenerBatchResultSource | null,
  cached: ScreenerBatchResultSource | undefined,
  assetType: 'stock' | 'etf' = 'stock',
) {
  const source = transient?.as_of === asOf && (transient.asset_type ?? 'stock') === assetType
    ? transient
    : cached?.as_of === asOf && (cached.asset_type ?? 'stock') === assetType
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
  assetType: 'stock' | 'etf' = 'stock',
) {
  return source?.as_of === asOf && (
    (source.ext_columns ?? '') !== (extColumns ?? '')
    || (source.asset_type ?? 'stock') !== assetType
  )
}

export function transientBatchColumnRefreshKey(
  source: ScreenerBatchResultSource | null,
  asOf: string,
  extColumns?: string,
  assetType: 'stock' | 'etf' = 'stock',
) {
  if (!shouldRefreshTransientBatchForColumns(source, asOf, extColumns, assetType)) return null
  return `${asOf}\u0000${assetType}\u0000${extColumns ?? ''}`
}

export function transientBatchColumnRetryParams(
  source: ScreenerBatchResultSource | null,
  asOf: string,
  extColumns?: string,
  assetType: 'stock' | 'etf' = 'stock',
) {
  if (!source || !transientBatchColumnRefreshKey(source, asOf, extColumns, assetType)) return null
  return {
    date: asOf,
    strategyIds: Object.keys(source.results),
    extColumns: extColumns ?? '',
  }
}
