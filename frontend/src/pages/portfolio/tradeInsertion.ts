export type TradeInsertionTarget = {
  tradeDate: string
  insertBeforeTradeId?: string
}

type TradeInsertionSource = {
  id: string
  trade_date: string
}

type InlineTradeSource = TradeInsertionSource & {
  account_id: string
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  price: number
}

export type InlineTradeDraft = {
  sourceTradeId: string
  accountId: string
  symbol: string
  tradeDate: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  insertBeforeTradeId?: string
}

export type InlineTradeCreatePayload = {
  account_id: string
  symbol: string
  trade_date: string
  side: 'buy' | 'sell'
  quantity: number
  price: number
  insert_before_trade_id?: string
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

/** 从被点击的明细复制可编辑字段，并固定该笔交易的插入上下文。 */
export function buildInlineTradeDraft(
  source: InlineTradeSource,
  target: TradeInsertionTarget,
): InlineTradeDraft {
  return {
    sourceTradeId: source.id,
    accountId: source.account_id,
    symbol: source.symbol,
    tradeDate: target.tradeDate,
    side: source.side,
    quantity: String(source.quantity),
    price: String(source.price),
    insertBeforeTradeId: target.insertBeforeTradeId,
  }
}

/** 构造不含 fee/tax 的请求体，让后端按当前费率自动估算费用。 */
export function buildInlineTradeCreatePayload(
  draft: InlineTradeDraft,
): InlineTradeCreatePayload | null {
  const quantity = Number(draft.quantity)
  const price = Number(draft.price)
  const symbol = draft.symbol.trim().toUpperCase()
  if (
    !draft.accountId || !symbol || !draft.tradeDate
    || draft.quantity.trim() === '' || !Number.isFinite(quantity) || !(quantity > 0)
    || draft.price.trim() === '' || !Number.isFinite(price) || !(price >= 0)
  ) return null

  return {
    account_id: draft.accountId,
    symbol,
    trade_date: draft.tradeDate,
    side: draft.side,
    quantity,
    price,
    ...(draft.insertBeforeTradeId
      ? { insert_before_trade_id: draft.insertBeforeTradeId }
      : {}),
  }
}
