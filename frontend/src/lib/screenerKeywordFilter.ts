export interface ScreenerKeywordRow {
  symbol?: unknown
  name?: unknown
}

export function matchesScreenerKeyword(row: ScreenerKeywordRow, keyword: string): boolean {
  const normalized = keyword.trim().toLowerCase()
  if (!normalized) return true

  const symbol = String(row.symbol ?? '').toLowerCase()
  const name = String(row.name ?? '').toLowerCase()
  return symbol.includes(normalized) || name.includes(normalized)
}
