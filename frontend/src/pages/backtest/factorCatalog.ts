import type { FactorColumn } from '@/lib/api'

export type FactorAssetType = 'stock' | 'etf'

export function factorColumnsForAsset(
  columns: readonly FactorColumn[],
  assetType: FactorAssetType,
): FactorColumn[] {
  return columns.filter(column => column.asset_types.includes(assetType))
}

export function factorNamesForAsset(
  columns: readonly FactorColumn[],
  factorNames: readonly string[],
  assetType: FactorAssetType,
): string[] {
  const supported = new Set(
    factorColumnsForAsset(columns, assetType).map(column => column.id),
  )
  return factorNames.filter(name => supported.has(name))
}
