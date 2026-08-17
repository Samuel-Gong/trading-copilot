export type TradeEstimateContext = {
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  price: number
}

type TradeEstimateDraft = {
  symbol: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
  tax: string
}

/** 只把仍匹配请求输入的估算结果合并到最新表单，避免异步响应覆盖用户后续修改。 */
export function mergeTradeEstimateIfCurrent<T extends TradeEstimateDraft>(
  current: T,
  requested: TradeEstimateContext,
  result: { fee: number; tax: number },
): T {
  if (
    current.symbol.trim().toUpperCase() !== requested.symbol
    || current.side !== requested.side
    || Number(current.quantity) !== requested.quantity
    || Number(current.price) !== requested.price
  ) return current
  return {
    ...current,
    fee: result.fee.toFixed(2),
    tax: result.tax.toFixed(2),
  }
}
