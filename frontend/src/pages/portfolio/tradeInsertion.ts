export type TradeInsertionTarget = {
  tradeDate: string
  insertBeforeTradeId?: string
}

type TradeInsertionSource = {
  id: string
  trade_date: string
}

/** 把倒序展示的交易行映射为每行之后的插入位置。 */
export function buildTradeInsertionTargets(
  items: readonly TradeInsertionSource[],
  fallbackTradeDate: string,
): TradeInsertionTarget[] {
  if (items.length === 0) return [{ tradeDate: fallbackTradeDate }]
  return items.map(item => ({
    tradeDate: item.trade_date,
    insertBeforeTradeId: item.id,
  }))
}
