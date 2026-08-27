export type TradeInsertionTarget = {
  tradeDate: string
  insertBeforeTradeId?: string
}

export type TradeInsertionSource = {
  id: string
  trade_date: string
}

type StockTradeGroupLike = {
  symbol: string
  name: string
}

export type InlineTradeSource = TradeInsertionSource & {
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

export type TradeLedgerView = 'flat' | 'stock' | 'date'

export type LedgerInlineTradeState = {
  view: TradeLedgerView
  draft: InlineTradeDraft
} | null

export type LedgerInlineTradeAction =
  | {
      type: 'start'
      view: TradeLedgerView
      source: InlineTradeSource
      target: TradeInsertionTarget
    }
  | { type: 'change'; draft: InlineTradeDraft }
  | { type: 'save-result'; saved: boolean }
  | { type: 'cancel' }
  | { type: 'context-changed'; context: 'account' | 'date' | 'view' }

export type InlineTradePersistenceResult =
  | { status: 'invalid' }
  | { status: 'failed'; error: unknown }
  | { status: 'saved'; refreshError?: unknown }

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

/** 在已加载的个股交易分组中按代码或名称做本地筛选。 */
export function filterStockTradeGroups<T extends StockTradeGroupLike>(
  groups: readonly T[],
  query: string,
): T[] {
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  if (!normalizedQuery) return [...groups]
  return groups.filter(group => (
    group.symbol.toLocaleLowerCase('zh-CN').includes(normalizedQuery)
    || group.name.toLocaleLowerCase('zh-CN').includes(normalizedQuery)
  ))
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

/** 管理流水表格内联草稿，使三个视图共享相同的开始、保存和清理语义。 */
export function reduceLedgerInlineTradeState(
  state: LedgerInlineTradeState,
  action: LedgerInlineTradeAction,
): LedgerInlineTradeState {
  switch (action.type) {
    case 'start':
      return {
        view: action.view,
        draft: buildInlineTradeDraft(action.source, action.target),
      }
    case 'change':
      return state ? { ...state, draft: action.draft } : state
    case 'save-result':
      return action.saved ? null : state
    case 'cancel':
    case 'context-changed':
      return null
  }
}

/** 执行内联补录；创建失败保留草稿，创建成功后再刷新持仓相关缓存。 */
export async function persistInlineTradeDraft(
  draft: InlineTradeDraft,
  dependencies: {
    createTrade: (payload: InlineTradeCreatePayload) => Promise<unknown>
    invalidate: () => Promise<unknown>
  },
): Promise<InlineTradePersistenceResult> {
  const payload = buildInlineTradeCreatePayload(draft)
  if (!payload) return { status: 'invalid' }

  try {
    await dependencies.createTrade(payload)
  } catch (error) {
    return { status: 'failed', error }
  }

  try {
    await dependencies.invalidate()
    return { status: 'saved' }
  } catch (refreshError) {
    // 交易已经落库，不能保留草稿供用户重试，否则可能重复创建。
    return { status: 'saved', refreshError }
  }
}
