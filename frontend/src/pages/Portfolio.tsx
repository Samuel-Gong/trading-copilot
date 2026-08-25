import { Fragment, useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  DndContext, closestCenter, KeyboardSensor, PointerSensor,
  useSensor, useSensors, type DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove, SortableContext, sortableKeyboardCoordinates,
  useSortable, verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  BellRing,
  BriefcaseBusiness,
  Check,
  CircleDollarSign,
  Edit3,
  GripVertical,
  History,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Trash2,
  Upload,
  X,
} from 'lucide-react'

import { DatePicker } from '@/components/DatePicker'
import { InstrumentSearchInput } from '@/components/InstrumentSearchInput'
import { Modal } from '@/components/Modal'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import {
  api,
  type PortfolioAccount,
  type PortfolioPosition,
  type PortfolioPriceMonitor,
  type PortfolioTrade,
} from '@/lib/api'
import { StatementImportDialog } from './portfolio/StatementImportDialog'
import { TradeCostDialog } from './portfolio/TradeCostDialog'
import { PositionMonitorDialog } from './portfolio/PositionMonitorDialog'
import {
  buildInlineTradeCreatePayload,
  buildInlineTradeDraft,
  buildTradeInsertionTargets,
  type InlineTradeDraft,
  type TradeInsertionTarget,
} from './portfolio/tradeInsertion'
import {
  mergeTradeEstimateIfCurrent,
  type TradeEstimateContext,
} from './portfolio/tradeEstimate'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { startAnalysis } from '@/lib/stockAnalysisStore'
import { useTradingDates } from '@/lib/useSharedQueries'

const INPUT_CLASS = 'w-full rounded-btn border border-border bg-elevated px-2.5 py-2 text-xs text-foreground outline-none focus:border-accent/60 disabled:opacity-60'

function localDateIso() {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function formatMoney(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

function formatPrice(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  }).format(value)
}

function formatQuantity(value: number) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value)
}

function formatRatio(value: number | null | undefined) {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

type TradeDraft = {
  accountId: string
  symbol: string
  tradeDate: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
  tax: string
  note: string
  insertBeforeTradeId?: string
}

type TradeGroupStats = {
  items: PortfolioTrade[]
  buyCount: number
  sellCount: number
  netAmount: number
}

type DateTradeGroup = TradeGroupStats & { date: string }

type StockTradeGroup = TradeGroupStats & {
  symbol: string
  name: string
  assetType: PortfolioTrade['asset_type']
  netQuantity: number
}

const WEEKDAY_LABELS = '日一二三四五六'

export function Portfolio() {
  const queryClient = useQueryClient()
  const [asOf, setAsOf] = useState('')
  const [accountId, setAccountId] = useState('all')
  const [accountName, setAccountName] = useState('')
  const [accountBusy, setAccountBusy] = useState(false)
  const [tradeBusy, setTradeBusy] = useState(false)
  const [reorderBusy, setReorderBusy] = useState(false)
  const [tradeEditBusy, setTradeEditBusy] = useState(false)
  const [costEditTrade, setCostEditTrade] = useState<PortfolioTrade | null>(null)
  const [monitorPosition, setMonitorPosition] = useState<PortfolioPosition | null>(null)
  const [tradeDetailPosition, setTradeDetailPosition] = useState<PortfolioPosition | null>(null)
  const [tradeDetailReturnPosition, setTradeDetailReturnPosition] = useState<PortfolioPosition | null>(null)
  const [draft, setDraft] = useState<TradeDraft | null>(null)
  const [statementOpen, setStatementOpen] = useState(false)
  const [tradesView, setTradesView] = useState<'flat' | 'stock' | 'date'>('flat')
  const tradingDatesQuery = useTradingDates()
  const tradingDates = tradingDatesQuery.data?.dates ?? []

  useEffect(() => {
    const latest = tradingDatesQuery.data?.latest_date
    if (!latest) return
    setAsOf(current => current && tradingDates.includes(current) ? current : latest)
  }, [tradingDates, tradingDatesQuery.data?.latest_date])

  const accountsQuery = useQuery({
    queryKey: QK.portfolioAccounts,
    queryFn: api.portfolioAccounts,
  })
  const selectedAccountId = accountId === 'all' ? undefined : accountId
  const snapshotQuery = useQuery({
    queryKey: QK.portfolioSnapshot(asOf, selectedAccountId),
    queryFn: () => api.portfolioSnapshot(asOf, selectedAccountId),
    enabled: Boolean(asOf),
  })
  const tradesQuery = useQuery({
    queryKey: QK.portfolioTrades(selectedAccountId),
    queryFn: () => api.portfolioTrades(selectedAccountId),
  })
  const priceMonitorsQuery = useQuery({
    queryKey: QK.portfolioPriceMonitors,
    queryFn: api.portfolioPriceMonitors,
  })
  const accounts = accountsQuery.data?.items ?? []
  const accountNameById = useMemo(
    () => Object.fromEntries(accounts.map(account => [account.id, account.name])),
    [accounts],
  )
  const snapshot = snapshotQuery.data
  const trades = tradesQuery.data?.items ?? []
  const visibleTrades = useMemo(
    () => asOf ? trades.filter(trade => trade.trade_date <= asOf) : [],
    [asOf, trades],
  )
  const rows = useMemo(() => (
    snapshot?.accounts.flatMap(account => account.positions.map(position => ({
      account,
      position,
    }))) ?? []
  ), [snapshot])
  const monitorBySymbol = useMemo(
    () => Object.fromEntries(
      (priceMonitorsQuery.data?.items ?? []).map(item => [item.symbol, item]),
    ) as Record<string, PortfolioPriceMonitor>,
    [priceMonitorsQuery.data?.items],
  )
  const missingStopLossCount = useMemo(() => {
    const symbols = new Set(rows.map(({ position }) => position.symbol))
    return [...symbols].filter(symbol => !monitorBySymbol[symbol]?.stop_loss_enabled).length
  }, [monitorBySymbol, rows])

  // trades 已由后端按 (trade_date, seq) 倒序返回，分组时保持数组顺序即为组内最新在前
  const tradesByDate = useMemo<DateTradeGroup[]>(() => {
    const byDate = new Map<string, DateTradeGroup>()
    for (const trade of visibleTrades) {
      let group = byDate.get(trade.trade_date)
      if (!group) {
        group = { date: trade.trade_date, items: [], buyCount: 0, sellCount: 0, netAmount: 0 }
        byDate.set(trade.trade_date, group)
      }
      group.items.push(trade)
      if (trade.side === 'buy') {
        group.buyCount += 1
        group.netAmount += trade.quantity * trade.price + trade.fee + trade.tax
      } else {
        group.sellCount += 1
        group.netAmount -= trade.quantity * trade.price - trade.fee - trade.tax
      }
    }
    return [...byDate.values()].sort((a, b) => b.date.localeCompare(a.date))
  }, [visibleTrades])

  const tradesByStock = useMemo<StockTradeGroup[]>(() => {
    const bySymbol = new Map<string, StockTradeGroup>()
    for (const trade of visibleTrades) {
      let group = bySymbol.get(trade.symbol)
      if (!group) {
        group = { symbol: trade.symbol, name: '', assetType: trade.asset_type, items: [], buyCount: 0, sellCount: 0, netAmount: 0, netQuantity: 0 }
        bySymbol.set(trade.symbol, group)
      }
      if (!group.name && trade.name) group.name = trade.name
      group.items.push(trade)
      if (trade.side === 'buy') {
        group.buyCount += 1
        group.netAmount += trade.quantity * trade.price + trade.fee + trade.tax
        group.netQuantity += trade.quantity
      } else {
        group.sellCount += 1
        group.netAmount -= trade.quantity * trade.price - trade.fee - trade.tax
        group.netQuantity -= trade.quantity
      }
    }
    return [...bySymbol.values()]
      .map(group => ({ ...group, name: group.name || group.symbol }))
      .sort((a, b) => {
        const aDate = a.items[0]?.trade_date ?? ''
        const bDate = b.items[0]?.trade_date ?? ''
        if (aDate !== bDate) return bDate.localeCompare(aDate)
        return a.symbol.localeCompare(b.symbol)
      })
  }, [visibleTrades])

  async function invalidatePortfolio() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: QK.portfolioAccounts }),
      queryClient.invalidateQueries({ queryKey: QK.portfolioWatchPool }),
      queryClient.invalidateQueries({ queryKey: ['portfolio-snapshot'] }),
      queryClient.invalidateQueries({ queryKey: ['portfolio-trades'] }),
    ])
  }

  async function invalidatePortfolioTradeChanges() {
    await Promise.all([
      invalidatePortfolio(),
      queryClient.invalidateQueries({ queryKey: QK.monitorRules }),
      queryClient.invalidateQueries({ queryKey: QK.portfolioPriceMonitors }),
    ])
  }

  async function createAccount() {
    const name = accountName.trim()
    if (!name) return
    setAccountBusy(true)
    try {
      const account = await api.portfolioAccountCreate(name)
      setAccountName('')
      setAccountId(account.id)
      await invalidatePortfolio()
      toast('账户已创建', 'success')
    } finally {
      setAccountBusy(false)
    }
  }

  async function renameAccount(account: PortfolioAccount) {
    const name = window.prompt('新的账户名称', account.name)?.trim()
    if (!name || name === account.name) return
    await api.portfolioAccountUpdate(account.id, name)
    await invalidatePortfolio()
    toast('账户已重命名', 'success')
  }

  async function deleteAccount(account: PortfolioAccount) {
    if (!window.confirm(`删除没有交易记录的账户“${account.name}”？`)) return
    await api.portfolioAccountDelete(account.id)
    if (accountId === account.id) setAccountId('all')
    await invalidatePortfolio()
    toast('账户已删除', 'success')
  }

  function openCreateTrade(
    side: 'buy' | 'sell' = 'buy',
    position?: PortfolioPosition,
    insertionTarget?: TradeInsertionTarget,
  ) {
    setTradeDetailReturnPosition(null)
    const tradeDate = insertionTarget?.tradeDate
      || asOf
      || tradingDatesQuery.data?.latest_date
      || localDateIso()
    setDraft({
      accountId: position?.account_id ?? selectedAccountId ?? accounts[0]?.id ?? '',
      symbol: position?.symbol ?? '',
      tradeDate,
      side,
      quantity: '',
      price: position?.current_price == null ? '' : String(position.current_price),
      fee: '',
      tax: '',
      note: '',
      insertBeforeTradeId: insertionTarget?.insertBeforeTradeId,
    })
  }

  function openCreateTradeFromDetail(
    position: PortfolioPosition,
    insertionTarget: TradeInsertionTarget,
  ) {
    openCreateTrade('buy', position, insertionTarget)
    setTradeDetailReturnPosition(position)
    setTradeDetailPosition(null)
  }

  function closeTradeDialog() {
    if (tradeBusy) return
    setDraft(null)
    if (tradeDetailReturnPosition) setTradeDetailPosition(tradeDetailReturnPosition)
    setTradeDetailReturnPosition(null)
  }

  async function saveTrade() {
    if (!draft) return
    const quantity = Number(draft.quantity)
    const price = Number(draft.price)
    // 费用/税费留空 => 不传字段，后端按费率配置自动估算；显式填 0 表示手工免佣/免税
    const fee = draft.fee.trim() === '' ? undefined : Number(draft.fee)
    const tax = draft.tax.trim() === '' ? undefined : Number(draft.tax)
    const symbol = draft.symbol.trim().toUpperCase()
    if (
      !draft.accountId || !symbol || !draft.tradeDate
      || !(quantity > 0) || !(price >= 0)
      || (fee !== undefined && !(fee >= 0)) || (tax !== undefined && !(tax >= 0))
    ) {
      toast('请填写账户、标的、交易日、正数量及有效的价格和费用', 'error')
      return
    }
    setTradeBusy(true)
    try {
      await api.portfolioTradeCreate({
        account_id: draft.accountId,
        symbol,
        trade_date: draft.tradeDate,
        side: draft.side,
        quantity,
        price,
        ...(fee === undefined ? {} : { fee }),
        ...(tax === undefined ? {} : { tax }),
        note: draft.note.trim(),
        ...(draft.insertBeforeTradeId
          ? { insert_before_trade_id: draft.insertBeforeTradeId }
          : {}),
      })
      const returnPosition = tradeDetailReturnPosition
      await invalidatePortfolioTradeChanges()
      setDraft(null)
      if (returnPosition) setTradeDetailPosition(returnPosition)
      setTradeDetailReturnPosition(null)
      toast(draft.side === 'buy' ? '买入交易已记录' : '卖出交易已记录', 'success')
    } finally {
      setTradeBusy(false)
    }
  }

  async function saveInlineTrade(inlineDraft: InlineTradeDraft) {
    const payload = buildInlineTradeCreatePayload(inlineDraft)
    if (!payload) {
      toast('请填写正数量及有效的成交价', 'error')
      return false
    }
    setTradeBusy(true)
    try {
      await api.portfolioTradeCreate(payload)
      await invalidatePortfolioTradeChanges()
      toast(inlineDraft.side === 'buy' ? '买入交易已记录' : '卖出交易已记录', 'success')
      return true
    } catch {
      return false
    } finally {
      setTradeBusy(false)
    }
  }

  async function deleteTrade(trade: PortfolioTrade) {
    if (!window.confirm(`删除 ${trade.trade_date} 的${trade.side === 'buy' ? '买入' : '卖出'}交易？持仓会重新计算。`)) return
    await api.portfolioTradeDelete(trade.id)
    await invalidatePortfolioTradeChanges()
    toast('交易已删除，历史持仓已重算', 'success')
  }

  async function reorderDayTrades(dayTradesInDisplayOrder: PortfolioTrade[]) {
    // dayTradesInDisplayOrder 是展示顺序 (晚成交在前)；API 要的是执行顺序 (最早成交在前)
    const executionOrderIds = [...dayTradesInDisplayOrder].reverse().map(item => item.id)
    setReorderBusy(true)
    try {
      await api.portfolioTradeReorder(executionOrderIds)
      // 日内顺序会影响 FIFO 批次和卖出校验，整个快照都要失效
      await invalidatePortfolio()
      toast('已调整该交易日的成交顺序', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '调整顺序失败', 'error')
    } finally {
      setReorderBusy(false)
    }
  }

  async function updateTradeExecution(
    trade: PortfolioTrade,
    quantity: number,
    price: number,
  ) {
    setTradeEditBusy(true)
    try {
      await api.portfolioTradeUpdateExecution(trade.id, { quantity, price })
      // 成交数量和价格都会影响 FIFO 批次、净买入与估值，整个快照都要失效
      await invalidatePortfolioTradeChanges()
      toast('成交数量和价格已更新', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '修改交易失败', 'error')
    } finally {
      setTradeEditBusy(false)
    }
  }

  async function updateTradeDate(trade: PortfolioTrade, tradeDate: string) {
    setTradeEditBusy(true)
    try {
      await api.portfolioTradeUpdateDate(trade.id, tradeDate)
      // 日期会改变回放顺序、历史持仓和交易分组，整个快照与流水都要失效
      await invalidatePortfolioTradeChanges()
      toast('交易日期已更新', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '修改交易日期失败', 'error')
    } finally {
      setTradeEditBusy(false)
    }
  }

  async function analyzePosition(position: PortfolioPosition) {
    const result = await startAnalysis(position.symbol, position.name, '', {
      accountId: position.account_id,
      sourceRef: `${position.account_id}:${position.symbol}`,
      asOf,
    })
    if (result.error) toast(result.error, 'error')
  }

  const unrealizedPositive = (snapshot?.unrealized_pnl ?? 0) >= 0
  const realizedPositive = (snapshot?.realized_pnl ?? 0) >= 0

  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  // trades 是后端排序数组 (交易日倒序，日内 seq 倒序)：同一天内靠上的行是更晚的成交
  // 只允许同一交易日内拖放排序；跨日放置不改变状态，行会弹回原位
  function handleTradeDragEnd(event: DragEndEvent) {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const activeTrade = visibleTrades.find(item => item.id === active.id)
    const overTrade = visibleTrades.find(item => item.id === over.id)
    if (!activeTrade || !overTrade || activeTrade.trade_date !== overTrade.trade_date) return
    const dayTrades = visibleTrades.filter(item => item.trade_date === activeTrade.trade_date)
    const oldIndex = dayTrades.findIndex(item => item.id === active.id)
    const newIndex = dayTrades.findIndex(item => item.id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    reorderDayTrades(arrayMove(dayTrades, oldIndex, newIndex))
  }

  return (
    <div className="min-h-full bg-base">
      <PageHeader
        title="持仓"
        subtitle="录入交易，按所选交易日回放历史持仓；成本口径为 FIFO"
        right={(
          <div className="flex items-center gap-2">
            <DatePicker
              value={asOf}
              onChange={setAsOf}
              min={tradingDatesQuery.data?.earliest_date ?? undefined}
              max={tradingDatesQuery.data?.latest_date ?? undefined}
              availableDates={tradingDates}
              disabled={tradingDates.length === 0}
              placeholder={tradingDatesQuery.isLoading ? '加载交易日…' : '选择交易日'}
            />
            <button
              onClick={() => Promise.all([snapshotQuery.refetch(), tradesQuery.refetch()])}
              disabled={!asOf}
              className="rounded-btn border border-border bg-surface p-2 text-secondary hover:text-foreground disabled:opacity-40"
              title="刷新持仓与交易"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', (snapshotQuery.isFetching || tradesQuery.isFetching) && 'animate-spin')} />
            </button>
          </div>
        )}
      />

      <main className="mx-auto max-w-[1500px] space-y-4 p-4 md:p-5">
        <section className="rounded-card border border-border bg-surface p-3">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex min-w-0 items-center gap-1 overflow-x-auto pb-1 lg:pb-0">
              <AccountPill active={accountId === 'all'} label="全部账户" onClick={() => setAccountId('all')} />
              {accounts.map(account => (
                <div key={account.id} className="flex shrink-0 items-center rounded-full bg-elevated">
                  <AccountPill active={accountId === account.id} label={account.name} onClick={() => setAccountId(account.id)} />
                  <button onClick={() => renameAccount(account)} className="p-1 text-muted hover:text-foreground" title="重命名账户"><Edit3 className="h-3 w-3" /></button>
                  <button onClick={() => deleteAccount(account)} className="mr-1 p-1 text-muted hover:text-danger" title="删除无交易账户"><Trash2 className="h-3 w-3" /></button>
                </div>
              ))}
            </div>
            <div className="flex gap-2">
              <input
                value={accountName}
                onChange={event => setAccountName(event.target.value)}
                onKeyDown={event => { if (event.key === 'Enter') createAccount() }}
                placeholder="新账户名称"
                className="h-8 w-36 rounded-btn border border-border bg-elevated px-2.5 text-xs outline-none focus:border-accent/60"
              />
              <button
                onClick={createAccount}
                disabled={accountBusy || !accountName.trim()}
                className="flex h-8 items-center gap-1 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-40"
              >
                {accountBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                新建账户
              </button>
            </div>
          </div>
        </section>

        {snapshotQuery.isLoading || !asOf ? (
          <div className="grid min-h-64 place-items-center rounded-card border border-border bg-surface"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
        ) : snapshotQuery.isError ? (
          <div className="rounded-card border border-danger/30 bg-danger/5 p-5 text-sm text-danger">持仓回放失败。检查交易流水后重试。</div>
        ) : (
          <>
            <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <MetricCard label="剩余持仓成本" value={`¥ ${formatMoney(snapshot?.total_cost)}`} detail={`${snapshot?.position_count ?? 0} 个持仓 · FIFO`} icon={BriefcaseBusiness} />
              <MetricCard label="总市值" value={`¥ ${formatMoney(snapshot?.market_value)}`} detail={`${snapshot?.priced_position_count ?? 0} 项有价格`} icon={CircleDollarSign} />
              <MetricCard label="浮动盈亏" value={`${unrealizedPositive ? '+' : ''}¥ ${formatMoney(snapshot?.unrealized_pnl)}`} detail={formatRatio(snapshot?.unrealized_return_ratio)} icon={CircleDollarSign} tone={unrealizedPositive ? 'up' : 'down'} />
              <MetricCard label="已实现盈亏" value={`${realizedPositive ? '+' : ''}¥ ${formatMoney(snapshot?.realized_pnl)}`} detail={`截至 ${asOf} · ${snapshot?.trade_count ?? 0} 笔`} icon={History} tone={realizedPositive ? 'up' : 'down'} />
            </section>

            {((snapshot?.missing_price_count ?? 0) > 0 || (snapshot?.stale_price_count ?? 0) > 0) && (
              <div className="flex items-start gap-2 rounded-card border border-warning/25 bg-warning/5 px-4 py-3 text-xs leading-5 text-secondary">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
                <span>
                  当前估值有 {snapshot?.missing_price_count ?? 0} 项缺少收盘价、{snapshot?.stale_price_count ?? 0} 项使用了更早交易日的收盘价；缺价持仓不计入市值和浮动盈亏。
                </span>
              </div>
            )}

            {!priceMonitorsQuery.isLoading && missingStopLossCount > 0 && (
              <div className="flex flex-wrap items-center gap-3 rounded-card border border-danger/30 bg-danger/5 px-4 py-3 text-xs text-secondary">
                <ShieldAlert className="h-4 w-4 shrink-0 text-danger" />
                <span className="min-w-0 flex-1">还有 {missingStopLossCount} 支持仓证券没有启用止损价格监控。止损价需要逐支设置，系统不会根据成本价自动猜测。</span>
                <span className="shrink-0 text-[10px] text-danger">在下方“价格监控”列处理</span>
              </div>
            )}

            {priceMonitorsQuery.isError && (
              <div className="flex items-center gap-2 rounded-card border border-warning/25 bg-warning/5 px-4 py-3 text-xs text-warning">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />价格监控状态加载失败，刷新后重试。
              </div>
            )}

            <section className="overflow-hidden rounded-card border border-border bg-surface">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
                <div><h2 className="text-sm font-medium">{asOf} 历史持仓</h2><p className="mt-0.5 text-[11px] text-muted">只回放交易日不晚于所选日期的流水；持仓不能直接编辑</p></div>
                <div className="flex items-center gap-2">
                  <button onClick={() => setStatementOpen(true)} disabled={accounts.length === 0} className="flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-3 text-xs text-secondary hover:text-foreground disabled:opacity-40"><Upload className="h-3.5 w-3.5" />导入交割单</button>
                  <button onClick={() => openCreateTrade('buy')} disabled={accounts.length === 0 || !asOf} className="flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-40"><Plus className="h-3.5 w-3.5" />记录交易</button>
                </div>
              </div>
              {accounts.length === 0 ? (
                <EmptyState title="先创建一个持仓账户" detail="账户只用于区分研究上下文，不连接券商或维护现金。" />
              ) : rows.length === 0 ? (
                <EmptyState title="这个日期还没有持仓" detail="录入买入和卖出交易后，系统会按日期回放持仓；未来交易不会出现在这里。" />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[1160px] text-left text-xs">
                    <thead className="bg-elevated/50 text-muted"><tr><th className="px-4 py-2 font-medium">标的 / 账户</th><th className="px-3 py-2 font-medium">剩余数量 / FIFO 成本</th><th className="px-3 py-2 font-medium">现价</th><th className="px-3 py-2 text-right font-medium">市值 / 浮动盈亏</th><th className="px-3 py-2 font-medium">价格监控</th><th className="px-4 py-2 text-right font-medium">操作</th></tr></thead>
                    <tbody className="divide-y divide-border/70">
                      {rows.map(({ account, position }) => (
                        <tr key={position.id} className="hover:bg-elevated/20">
                          <td className="px-4 py-3"><div className="font-medium">{position.name || position.symbol}</div><div className="mt-0.5 font-mono text-[11px] text-muted">{position.symbol} · {account.name}</div>{position.note && <div className="mt-1 max-w-xs truncate text-[11px] text-secondary">{position.note}</div>}</td>
                          <td className="px-3 py-3 tabular"><div>{formatQuantity(position.quantity)}</div><div className="mt-0.5 text-muted">¥ {formatMoney(position.average_cost)}</div><div className="mt-0.5 text-[10px] text-muted">最早剩余批次 {position.purchase_date}</div></td>
                          <td className="px-3 py-3"><PositionPrice position={position} /></td>
                          <td className="px-3 py-3 text-right tabular"><div>{position.market_value == null ? '不可估值' : `¥ ${formatMoney(position.market_value)}`}</div><div className={cn('mt-0.5', position.unrealized_pnl == null ? 'text-muted' : position.unrealized_pnl >= 0 ? 'text-bull' : 'text-bear')}>{position.unrealized_pnl == null ? '—' : `${position.unrealized_pnl >= 0 ? '+' : ''}¥ ${formatMoney(position.unrealized_pnl)} · ${formatRatio(position.unrealized_return_ratio)}`}</div></td>
                          <td className="px-3 py-3"><PositionMonitorCell monitor={monitorBySymbol[position.symbol]} loading={priceMonitorsQuery.isLoading} onOpen={() => setMonitorPosition(position)} /></td>
                          <td className="px-4 py-3"><div className="flex justify-end gap-1"><button onClick={() => setTradeDetailPosition(position)} className="flex items-center gap-1 rounded-btn border border-border bg-elevated/40 px-2 py-1.5 text-[11px] text-secondary hover:text-foreground" title={`查看 ${position.name || position.symbol} 的交易明细`}><History className="h-3.5 w-3.5" />明细</button><button onClick={() => analyzePosition(position)} className="flex items-center gap-1 rounded-btn border border-accent/25 bg-accent/5 px-2 py-1.5 text-[11px] text-accent hover:bg-accent/10" title={`分析 ${asOf} 的持仓`}><Sparkles className="h-3.5 w-3.5" />分析</button><button onClick={() => openCreateTrade('buy', position)} className="flex items-center gap-1 rounded-btn border border-bull/20 bg-bull/5 px-2 py-1.5 text-[11px] text-bull"><ArrowDownLeft className="h-3.5 w-3.5" />买入</button><button onClick={() => openCreateTrade('sell', position)} className="flex items-center gap-1 rounded-btn border border-bear/20 bg-bear/5 px-2 py-1.5 text-[11px] text-bear"><ArrowUpRight className="h-3.5 w-3.5" />卖出</button></div></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}

        <section className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
            <div><h2 className="text-sm font-medium">交易流水</h2><p className="mt-0.5 text-[11px] text-muted">同一交易日内的多笔交易可拖动行首手柄调整成交先后，影响持仓回放；删除交易会重算全部历史持仓</p></div>
            <div className="flex items-center gap-1">
              {(['flat', 'stock', 'date'] as const).map(v => (
                <button
                  key={v}
                  onClick={() => setTradesView(v)}
                  className={`px-3 py-1.5 text-xs font-medium border-b-2 transition-colors cursor-pointer ${
                    tradesView === v ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground'
                  }`}
                >
                  {v === 'flat' ? '全部流水' : v === 'stock' ? '按个股' : '按日期'}
                </button>
              ))}
            </div>
            <span className="font-mono text-[11px] text-muted">{visibleTrades.length} 笔</span>
          </div>
          {tradesQuery.isLoading ? (
            <div className="grid min-h-36 place-items-center"><Loader2 className="h-4 w-4 animate-spin text-muted" /></div>
          ) : tradesQuery.isError ? (
            <div className="grid min-h-36 place-items-center gap-2 px-4 text-center text-xs text-danger">
              <span>交易流水加载失败</span>
              <button
                type="button"
                onClick={() => tradesQuery.refetch()}
                className="rounded-btn border border-border px-3 py-1.5 text-secondary hover:text-foreground"
              >
                重试
              </button>
            </div>
          ) : visibleTrades.length === 0 ? (
            <div className="grid min-h-36 place-items-center px-4 text-center text-xs text-muted">尚无交易记录</div>
          ) : tradesView === 'date' ? (
            <div className="space-y-3 p-3">
              {tradesByDate.map(group => (
                <div key={group.date} className="overflow-hidden rounded-card border border-border bg-surface">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-elevated/40 px-3.5 py-2.5">
                    <div className="flex items-baseline gap-2">
                      <div className="font-mono text-sm font-medium">{group.date}</div>
                      <span className="text-[11px] text-muted">周{WEEKDAY_LABELS[new Date(`${group.date}T00:00:00`).getDay()]}</span>
                    </div>
                    <div className="text-[11px] text-muted">
                      {group.items.length} 笔 · 买 {group.buyCount} / 卖 {group.sellCount} · 净买入 <SignedNetAmount value={group.netAmount} />（含费用税）
                    </div>
                  </div>
                  <GroupedTradeTable items={group.items} mode="byDate" accountNameById={accountNameById} onDelete={deleteTrade} onReorderDay={reorderDayTrades} reorderBusy={reorderBusy} onEditCost={setCostEditTrade} onUpdateExecution={updateTradeExecution} onUpdateDate={updateTradeDate} tradeEditBusy={tradeEditBusy} tradingDates={tradingDates} earliestTradingDate={tradingDatesQuery.data?.earliest_date} latestTradingDate={tradingDatesQuery.data?.latest_date} />
                </div>
              ))}
            </div>
          ) : tradesView === 'stock' ? (
            <div className="space-y-3 p-3">
              {tradesByStock.map(group => (
                <div key={group.symbol} className="overflow-hidden rounded-card border border-border bg-surface">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-elevated/40 px-3.5 py-2.5">
                    <div>
                      <div className="text-sm font-medium">{group.name}</div>
                      <div className="font-mono text-[11px] text-muted">{group.symbol}</div>
                    </div>
                    <div className="text-[11px] text-muted">
                      买 {group.buyCount} / 卖 {group.sellCount} · 净买入 <SignedNetAmount value={group.netAmount} />（含费用税） · 净股数 <span className={`font-mono ${group.netQuantity > 0 ? 'text-foreground' : group.netQuantity < 0 ? 'text-bear' : 'text-muted'}`}>{formatQuantity(group.netQuantity)}</span>
                    </div>
                  </div>
                  <GroupedTradeTable items={group.items} mode="byStock" accountNameById={accountNameById} onDelete={deleteTrade} onEditCost={setCostEditTrade} onUpdateExecution={updateTradeExecution} onUpdateDate={updateTradeDate} tradeEditBusy={tradeEditBusy} tradingDates={tradingDates} earliestTradingDate={tradingDatesQuery.data?.earliest_date} latestTradingDate={tradingDatesQuery.data?.latest_date} />
                </div>
              ))}
            </div>
          ) : (
            <DndContext sensors={dndSensors} collisionDetection={closestCenter} onDragEnd={handleTradeDragEnd}>
              <SortableContext items={visibleTrades.map(trade => trade.id)} strategy={verticalListSortingStrategy}>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[900px] text-left text-xs">
                    <thead className="bg-elevated/50 text-muted"><tr><th className="w-7" /><th className="px-4 py-2 font-medium">交易日</th><th className="px-3 py-2 font-medium">标的 / 账户</th><th className="px-3 py-2 font-medium">方向</th><th className="px-3 py-2 text-right font-medium">成交价 × 数量</th><th className="px-3 py-2 text-right font-medium">费用 / 税费</th><th className="px-3 py-2 font-medium">备注</th><th className="px-4 py-2 text-right font-medium">操作</th></tr></thead>
                    <tbody className="divide-y divide-border/70">
                      {visibleTrades.map(trade => (
                        <SortableTradeRow key={trade.id} id={trade.id} busy={reorderBusy}>
                          <td className="px-4 py-3 font-mono"><TradeDateCell trade={trade} busy={tradeEditBusy} tradingDates={tradingDates} earliestTradingDate={tradingDatesQuery.data?.earliest_date} latestTradingDate={tradingDatesQuery.data?.latest_date} onSave={updateTradeDate} /></td>
                          <td className="px-3 py-3"><div>{trade.name || trade.symbol}</div><div className="font-mono text-[10px] text-muted">{trade.symbol} · {accountNameById[trade.account_id] || '未知账户'}</div></td>
                          <TradeRowCells trade={trade} onDelete={deleteTrade} onEditCost={setCostEditTrade} onUpdateExecution={updateTradeExecution} tradeEditBusy={tradeEditBusy} />
                        </SortableTradeRow>
                      ))}
                    </tbody>
                  </table>
                </div>
              </SortableContext>
            </DndContext>
          )}
        </section>
      </main>

      {draft && (
        <TradeDialog
          accounts={accounts}
          draft={draft}
          busy={tradeBusy}
          tradingDates={tradingDates}
          earliestTradingDate={tradingDatesQuery.data?.earliest_date}
          latestTradingDate={tradingDatesQuery.data?.latest_date}
          onChange={setDraft}
          onClose={closeTradeDialog}
          onSave={saveTrade}
        />
      )}
      {statementOpen && (
        <StatementImportDialog
          accounts={accounts}
          defaultAccountId={selectedAccountId ?? accounts[0]?.id ?? ''}
          onClose={() => setStatementOpen(false)}
          onCommitted={invalidatePortfolioTradeChanges}
        />
      )}
      {tradeDetailPosition && (
        <PositionTradesDialog
          position={tradeDetailPosition}
          accountName={accountNameById[tradeDetailPosition.account_id] || '未知账户'}
          items={visibleTrades.filter(trade => (
            trade.account_id === tradeDetailPosition.account_id
            && trade.symbol === tradeDetailPosition.symbol
          ))}
          loading={tradesQuery.isLoading}
          loadError={tradesQuery.isError}
          reorderBusy={reorderBusy}
          tradeEditBusy={tradeEditBusy}
          tradeBusy={tradeBusy}
          defaultTradeDate={asOf || tradingDatesQuery.data?.latest_date || localDateIso()}
          tradingDates={tradingDates}
          earliestTradingDate={tradingDatesQuery.data?.earliest_date}
          latestTradingDate={tradingDatesQuery.data?.latest_date}
          onClose={() => setTradeDetailPosition(null)}
          onCreateTrade={target => openCreateTradeFromDetail(tradeDetailPosition, target)}
          onSaveInlineTrade={saveInlineTrade}
          onDelete={deleteTrade}
          onReorderDay={reorderDayTrades}
          onRetry={() => tradesQuery.refetch()}
          onUpdateExecution={updateTradeExecution}
          onUpdateDate={updateTradeDate}
        />
      )}
      {costEditTrade && (
        <TradeCostDialog
          trade={costEditTrade}
          onClose={() => setCostEditTrade(null)}
          onSaved={invalidatePortfolio}
        />
      )}
      {monitorPosition && (
        <PositionMonitorDialog
          key={monitorPosition.symbol}
          position={monitorPosition}
          monitor={monitorBySymbol[monitorPosition.symbol]}
          onClose={() => setMonitorPosition(null)}
        />
      )}
    </div>
  )
}

function AccountPill({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return <button onClick={onClick} className={cn('shrink-0 rounded-full px-3 py-1.5 text-xs transition-colors', active ? 'bg-accent text-white' : 'text-secondary hover:text-foreground')}>{label}</button>
}

function MetricCard({ label, value, detail, icon: Icon, tone = 'neutral' }: { label: string; value: string; detail: string; icon: ComponentType<{ className?: string }>; tone?: 'neutral' | 'up' | 'down' | 'warning' }) {
  const toneClass = tone === 'up' ? 'text-bull' : tone === 'down' ? 'text-bear' : tone === 'warning' ? 'text-warning' : 'text-foreground'
  return <div className="rounded-card border border-border bg-surface p-4"><div className="flex items-center justify-between text-xs text-muted"><span>{label}</span><Icon className="h-4 w-4" /></div><div className={cn('mt-3 truncate font-mono text-lg font-semibold tabular', toneClass)}>{value}</div><div className="mt-1 text-[11px] text-muted">{detail}</div></div>
}

function PositionPrice({ position }: { position: PortfolioPosition }) {
  if (!position.price_available) {
    return <div title="估值日前 365 天内没有可用的本地收盘价；该持仓不计入市值和浮动盈亏"><div className="font-mono text-secondary">—</div><div className="mt-0.5 text-[10px] text-danger">缺少收盘价</div></div>
  }
  return <div title={position.price_stale ? '现价使用估值日前最近可用的本地收盘价' : '现价使用估值日当天的本地收盘价'}><div className="font-mono text-foreground">¥ {formatMoney(position.current_price)}</div><div className={cn('mt-0.5 text-[10px]', position.price_stale ? 'text-warning' : 'text-muted')}>收盘价 · {position.price_date}{position.price_stale && ' · 滞后'}</div></div>
}

function PositionMonitorCell({ monitor, loading, onOpen }: { monitor?: PortfolioPriceMonitor; loading: boolean; onOpen: () => void }) {
  if (loading) return <div className="flex items-center gap-1.5 text-[10px] text-muted"><Loader2 className="h-3 w-3 animate-spin" />加载中</div>
  if (!monitor?.stop_loss_enabled) {
    return (
      <button onClick={onOpen} className="inline-flex items-center gap-1.5 rounded-btn border border-danger/30 bg-danger/5 px-2.5 py-1.5 text-[11px] font-medium text-danger hover:bg-danger/10">
        <ShieldAlert className="h-3.5 w-3.5" />{monitor?.stop_loss_price ? '启用止损' : '设置止损'}
      </button>
    )
  }
  return (
    <button onClick={onOpen} className="group min-w-[132px] rounded-btn border border-border/80 bg-base/40 px-2.5 py-1.5 text-left hover:border-accent/35 hover:bg-elevated/60">
      <span className="flex items-center gap-1.5 text-[11px] text-foreground"><BellRing className="h-3 w-3 text-danger" />止损 ¥ {formatPrice(monitor.stop_loss_price)}</span>
      <span className="mt-0.5 block pl-4 text-[10px] text-muted">{monitor.add_position_enabled && monitor.add_position_price != null ? `加仓 ¥ ${formatPrice(monitor.add_position_price)}` : '未设置加仓价'} · <span className="group-hover:text-accent">编辑</span></span>
    </button>
  )
}

function TradeDateCell({
  trade,
  busy,
  tradingDates,
  earliestTradingDate,
  latestTradingDate,
  onSave,
}: {
  trade: PortfolioTrade
  busy: boolean
  tradingDates: readonly string[]
  earliestTradingDate?: string | null
  latestTradingDate?: string | null
  onSave: (trade: PortfolioTrade, tradeDate: string) => void
}) {
  const [editing, setEditing] = useState(false)

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <DatePicker
          value={trade.trade_date}
          onChange={tradeDate => {
            setEditing(false)
            if (tradeDate !== trade.trade_date) onSave(trade, tradeDate)
          }}
          min={earliestTradingDate ?? undefined}
          max={latestTradingDate ?? localDateIso()}
          availableDates={tradingDates.length > 0 ? tradingDates : undefined}
          disabled={busy}
          align="left"
          buttonClassName="!h-7 !px-1.5 font-mono"
        />
        <button type="button" onClick={() => setEditing(false)} disabled={busy} className="rounded-btn p-0.5 text-muted hover:bg-elevated hover:text-foreground" title="取消修改"><X className="h-3 w-3" /></button>
      </span>
    )
  }

  return (
    <span className="inline-flex items-center gap-1">
      <span>{trade.trade_date}</span>
      <button type="button" onClick={() => setEditing(true)} disabled={busy} className="rounded-btn p-0.5 text-muted hover:text-foreground" title="修改交易日期"><Pencil className="h-3 w-3" /></button>
    </span>
  )
}

function TradeExecutionCell({
  trade,
  busy,
  onSave,
}: {
  trade: PortfolioTrade
  busy: boolean
  onSave: (trade: PortfolioTrade, quantity: number, price: number) => void
}) {
  const [editing, setEditing] = useState(false)
  const [quantityValue, setQuantityValue] = useState('')
  const [priceValue, setPriceValue] = useState('')

  function open() {
    setQuantityValue(String(trade.quantity))
    setPriceValue(String(trade.price))
    setEditing(true)
  }

  function save() {
    const quantity = Number(quantityValue)
    const price = Number(priceValue)
    if (!Number.isFinite(quantity) || quantity <= 0 || !Number.isFinite(price) || price <= 0) {
      toast('成交数量和成交价必须为正数', 'error')
      return
    }
    const changed = Math.abs(quantity - trade.quantity) >= 1e-9
      || Math.abs(price - trade.price) >= 1e-9
    if (changed) onSave(trade, quantity, price)
    setEditing(false)
  }

  if (editing) {
    return (
      <div className="inline-flex items-center justify-end gap-1">
        <span>¥</span>
        <input
          aria-label="成交价"
          type="number"
          step="0.001"
          min="0"
          value={priceValue}
          autoFocus
          disabled={busy}
          onChange={event => setPriceValue(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') save()
            if (event.key === 'Escape') setEditing(false)
          }}
          className={`${INPUT_CLASS} !w-20 !px-1 !py-0.5 text-right font-mono`}
        />
        <span>×</span>
        <input
          aria-label="成交数量"
          type="number"
          step="any"
          min="0"
          value={quantityValue}
          disabled={busy}
          onChange={event => setQuantityValue(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') save()
            if (event.key === 'Escape') setEditing(false)
          }}
          className={`${INPUT_CLASS} !w-16 !px-1 !py-0.5 text-right font-mono`}
        />
        <button type="button" onClick={save} disabled={busy} className="rounded-btn p-0.5 text-accent hover:bg-accent/10" title="保存数量和成交价"><Check className="h-3 w-3" /></button>
        <button type="button" onClick={() => setEditing(false)} disabled={busy} className="rounded-btn p-0.5 text-muted hover:bg-elevated hover:text-foreground" title="取消修改"><X className="h-3 w-3" /></button>
      </div>
    )
  }

  return (
    <span className="inline-flex items-center justify-end gap-1">
      <span>¥ {formatPrice(trade.price)} × {formatQuantity(trade.quantity)}</span>
      <button type="button" onClick={open} disabled={busy} className="rounded-btn p-0.5 text-muted hover:text-foreground" title="修改成交数量和成交价"><Pencil className="h-3 w-3" /></button>
    </span>
  )
}

function SortableTradeRow({ id, busy, children, trade, insertionTarget, onInsertTrade, insertionDisabled }: { id: string; busy: boolean; children: ReactNode; trade?: PortfolioTrade; insertionTarget?: TradeInsertionTarget; onInsertTrade?: (trade: PortfolioTrade, target: TradeInsertionTarget) => void; insertionDisabled?: boolean }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id, disabled: busy })
  return (
    <tr
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn('group/trade hover:bg-elevated/20', isDragging && 'bg-elevated/30 opacity-50')}
    >
      <td className="relative w-7 pl-2 pr-0">
        <button
          {...attributes}
          {...listeners}
          disabled={busy}
          className="cursor-grab touch-none rounded-btn p-1 text-muted hover:bg-elevated hover:text-foreground active:cursor-grabbing disabled:opacity-40"
          title="拖动调整当日成交顺序"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </button>
        {trade && insertionTarget && onInsertTrade && (
          <TradeInsertionButton trade={trade} target={insertionTarget} onInsert={onInsertTrade} disabled={Boolean(insertionDisabled)} />
        )}
      </td>
      {children}
    </tr>
  )
}

function TradeRowCells({ trade, onDelete, onEditCost, onUpdateExecution, tradeEditBusy, interactionDisabled }: { trade: PortfolioTrade; onDelete: (trade: PortfolioTrade) => void; onEditCost?: (trade: PortfolioTrade) => void; onUpdateExecution?: (trade: PortfolioTrade, quantity: number, price: number) => void; tradeEditBusy?: boolean; interactionDisabled?: boolean }) {
  const editingDisabled = Boolean(tradeEditBusy || interactionDisabled)
  return (
    <>
      <td className="px-3 py-3"><TradeSide side={trade.side} migrated={trade.migration_source === 'legacy_position'} /></td>
      <td className="px-3 py-3 text-right font-mono">{onUpdateExecution ? <TradeExecutionCell trade={trade} busy={editingDisabled} onSave={onUpdateExecution} /> : <>¥ {formatPrice(trade.price)} × {formatQuantity(trade.quantity)}</>}</td>
      <td className="px-3 py-3 text-right font-mono">¥ {formatMoney(trade.fee)} / ¥ {formatMoney(trade.tax)}<CostSourceChip source={trade.cost_source} />{onEditCost && <button onClick={() => onEditCost(trade)} disabled={interactionDisabled} className="ml-0.5 rounded-btn p-1 align-middle text-muted hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40" title="修改费用/税费"><Edit3 className="h-3 w-3" /></button>}</td>
      <td className="max-w-xs truncate px-3 py-3 text-secondary">{trade.note || '—'}</td>
      <td className="px-4 py-3 text-right">
        <div className="flex items-center justify-end gap-0.5">
          <button onClick={() => onDelete(trade)} disabled={interactionDisabled} className="rounded-btn p-1.5 text-muted hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40" title="删除交易并重算持仓"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      </td>
    </>
  )
}

function TradeSide({ side, migrated }: { side: 'buy' | 'sell'; migrated: boolean }) {
  const buying = side === 'buy'
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]', buying ? 'border-bull/20 bg-bull/5 text-bull' : 'border-bear/20 bg-bear/5 text-bear')}>
      {buying ? <ArrowDownLeft className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />}
      {migrated ? '期初买入（迁移）' : buying ? '买入' : '卖出'}
    </span>
  )
}

function CostSourceChip({ source }: { source?: PortfolioTrade['cost_source'] }) {
  if (source === 'estimated') {
    return <span className="ml-1.5 inline-flex items-center rounded border border-border bg-elevated px-1 py-px text-[9px] text-muted" title="费用/税费按费率配置自动估算，可导入交割单校准">估</span>
  }
  if (source === 'imported' || source === 'calibrated') {
    return <span className="ml-1.5 inline-flex items-center rounded border border-border bg-elevated px-1 py-px text-[9px] text-muted" title="费用/税费来自交割单">交割单</span>
  }
  return null
}

function SignedNetAmount({ value }: { value: number }) {
  const tone = value > 0 ? 'text-bull' : value < 0 ? 'text-bear' : 'text-muted'
  return <span className={`font-mono ${tone}`}>{value > 0 ? '+' : value < 0 ? '-' : ''}¥ {formatMoney(Math.abs(value))}</span>
}

function PositionTradesDialog({
  position,
  accountName,
  items,
  loading,
  loadError,
  reorderBusy,
  tradeEditBusy,
  tradeBusy,
  defaultTradeDate,
  tradingDates,
  earliestTradingDate,
  latestTradingDate,
  onClose,
  onCreateTrade,
  onSaveInlineTrade,
  onDelete,
  onReorderDay,
  onRetry,
  onUpdateExecution,
  onUpdateDate,
}: {
  position: PortfolioPosition
  accountName: string
  items: PortfolioTrade[]
  loading: boolean
  loadError: boolean
  reorderBusy: boolean
  tradeEditBusy: boolean
  tradeBusy: boolean
  defaultTradeDate: string
  tradingDates: readonly string[]
  earliestTradingDate?: string | null
  latestTradingDate?: string | null
  onClose: () => void
  onCreateTrade: (target: TradeInsertionTarget) => void
  onSaveInlineTrade: (draft: InlineTradeDraft) => Promise<boolean>
  onDelete: (trade: PortfolioTrade) => void
  onReorderDay: (dayTradesInDisplayOrder: PortfolioTrade[]) => void
  onRetry: () => void
  onUpdateExecution: (trade: PortfolioTrade, quantity: number, price: number) => void
  onUpdateDate: (trade: PortfolioTrade, tradeDate: string) => void
}) {
  const [inlineDraft, setInlineDraft] = useState<InlineTradeDraft | null>(null)
  const groups = useMemo(() => {
    const byDate = new Map<string, PortfolioTrade[]>()
    for (const trade of items) {
      const group = byDate.get(trade.trade_date)
      if (group) group.push(trade)
      else byDate.set(trade.trade_date, [trade])
    }
    return [...byDate.entries()].map(([date, dayItems]) => ({
      date,
      items: dayItems,
      insertionTargets: buildTradeInsertionTargets(dayItems, date),
    }))
  }, [items])
  const accountNameById = useMemo(
    () => ({ [position.account_id]: accountName }),
    [accountName, position.account_id],
  )
  const interactionDisabled = Boolean(inlineDraft) || tradeBusy || reorderBusy || tradeEditBusy

  function startInlineTrade(trade: PortfolioTrade, target: TradeInsertionTarget) {
    if (interactionDisabled) return
    setInlineDraft(buildInlineTradeDraft(trade, target))
  }

  async function saveInlineDraft() {
    if (!inlineDraft || tradeBusy) return
    if (await onSaveInlineTrade(inlineDraft)) setInlineDraft(null)
  }

  function closeIfIdle() {
    if (!tradeBusy) onClose()
  }

  return (
    <Modal
      onClose={closeIfIdle}
      labelledBy="position-trades-title"
      panelClassName="flex max-h-[92vh] w-[96vw] max-w-6xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-3 backdrop-blur-sm sm:p-4"
    >
      <header className="flex items-center gap-3 border-b border-border px-4 py-3.5 sm:px-5">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-btn border border-accent/25 bg-accent/10 text-accent">
          <History className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 id="position-trades-title" className="truncate text-sm font-semibold text-foreground">
            {position.name || position.symbol} · 交易明细
          </h2>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
            <span className="font-mono">{position.symbol}</span>
            <span>{accountName}</span>
            <span>{items.length} 笔</span>
          </div>
        </div>
        <button onClick={closeIfIdle} disabled={tradeBusy} className="rounded-btn p-1.5 text-muted hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40" title="关闭">
          <X className="h-4 w-4" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-4">
        <p className="mb-3 text-[11px] leading-5 text-muted">
          将鼠标移到任意明细行，行下边界会出现加号；点击后可直接补录，费用在保存时自动计算。修改或删除后会重新计算全部历史持仓。
        </p>
        {loading ? (
          <div className="grid min-h-40 place-items-center">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : loadError ? (
          <div className="grid min-h-40 place-items-center gap-2 rounded-card border border-danger/25 px-4 text-center text-xs text-danger">
            <span>交易明细加载失败</span>
            <button
              type="button"
              onClick={onRetry}
              className="rounded-btn border border-border px-3 py-1.5 text-secondary hover:text-foreground"
            >
              重试
            </button>
          </div>
        ) : groups.length === 0 ? (
          <div className="grid min-h-40 place-items-center rounded-card border border-dashed border-border text-center text-xs text-muted">
            <div>
              <div>暂无交易明细</div>
              <button
                type="button"
                onClick={() => onCreateTrade(buildTradeInsertionTargets([], defaultTradeDate)[0])}
                className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-btn border border-accent/25 bg-accent/5 px-3 text-xs text-accent hover:bg-accent/10"
              >
                <Plus className="h-3.5 w-3.5" />添加第一条明细
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {groups.map(group => (
              <section key={group.date} className="overflow-hidden rounded-card border border-border">
                <div className="flex items-center justify-between gap-3 border-b border-border bg-elevated/40 px-3.5 py-2.5">
                  <div className="font-mono text-xs font-medium text-foreground">{group.date}</div>
                  <div className="text-[10px] text-muted">
                    {group.items.length} 笔{group.items.length > 1 ? ' · 可拖动排序' : ''}
                  </div>
                </div>
                <GroupedTradeTable
                  items={group.items}
                  mode="byDate"
                  accountNameById={accountNameById}
                  onDelete={onDelete}
                  onReorderDay={group.items.length > 1 ? onReorderDay : undefined}
                  reorderBusy={reorderBusy}
                  onUpdateExecution={onUpdateExecution}
                  onUpdateDate={onUpdateDate}
                  tradeEditBusy={tradeEditBusy}
                  tradingDates={tradingDates}
                  earliestTradingDate={earliestTradingDate}
                  latestTradingDate={latestTradingDate}
                  insertionTargets={group.insertionTargets}
                  onInsertTrade={startInlineTrade}
                  inlineDraft={inlineDraft}
                  onInlineDraftChange={setInlineDraft}
                  onSaveInlineDraft={saveInlineDraft}
                  inlineSaveBusy={tradeBusy}
                  interactionDisabled={interactionDisabled}
                />
              </section>
            ))}
          </div>
        )}
      </div>
    </Modal>
  )
}

function GroupedTradeTable({ items, mode, accountNameById, onDelete, onReorderDay, reorderBusy, onEditCost, onUpdateExecution, onUpdateDate, tradeEditBusy, tradingDates, earliestTradingDate, latestTradingDate, insertionTargets, onInsertTrade, inlineDraft, onInlineDraftChange, onSaveInlineDraft, inlineSaveBusy, interactionDisabled }: { items: PortfolioTrade[]; mode: 'byDate' | 'byStock'; accountNameById: Record<string, string>; onDelete: (trade: PortfolioTrade) => void; onReorderDay?: (dayTradesInDisplayOrder: PortfolioTrade[]) => void; reorderBusy?: boolean; onEditCost?: (trade: PortfolioTrade) => void; onUpdateExecution?: (trade: PortfolioTrade, quantity: number, price: number) => void; onUpdateDate: (trade: PortfolioTrade, tradeDate: string) => void; tradeEditBusy?: boolean; tradingDates: readonly string[]; earliestTradingDate?: string | null; latestTradingDate?: string | null; insertionTargets?: TradeInsertionTarget[]; onInsertTrade?: (trade: PortfolioTrade, target: TradeInsertionTarget) => void; inlineDraft?: InlineTradeDraft | null; onInlineDraftChange?: (draft: InlineTradeDraft | null) => void; onSaveInlineDraft?: () => void; inlineSaveBusy?: boolean; interactionDisabled?: boolean }) {
  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  // byDate 卡片内 items 即当日全部交易（展示顺序），可在组内拖放排序；byStock 组跨日期不支持调整
  const sortable = mode === 'byDate' && Boolean(onReorderDay)

  function handleDragEnd(event: DragEndEvent) {
    if (!onReorderDay || interactionDisabled) return
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIndex = items.findIndex(item => item.id === active.id)
    const newIndex = items.findIndex(item => item.id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    onReorderDay(arrayMove(items, oldIndex, newIndex))
  }

  const renderFirstCells = (trade: PortfolioTrade) => (
    <>
      <td className="px-4 py-3 font-mono">
        <TradeDateCell
          trade={trade}
          busy={Boolean(tradeEditBusy || interactionDisabled)}
          tradingDates={tradingDates}
          earliestTradingDate={earliestTradingDate}
          latestTradingDate={latestTradingDate}
          onSave={onUpdateDate}
        />
      </td>
      {mode === 'byDate' ? (
        <td className="px-3 py-3"><div>{trade.name || trade.symbol}</div><div className="font-mono text-[10px] text-muted">{trade.symbol} · {accountNameById[trade.account_id] || '未知账户'}</div></td>
      ) : (
        <td className="px-3 py-3 text-muted">{accountNameById[trade.account_id] || '未知账户'}</td>
      )}
    </>
  )

  const renderInlineDraft = (trade: PortfolioTrade) => (
    inlineDraft?.sourceTradeId === trade.id && onInlineDraftChange && onSaveInlineDraft
      ? (
        <InlineTradeDraftRow
          draft={inlineDraft}
          mode={mode}
          accountName={accountNameById[inlineDraft.accountId] || '未知账户'}
          busy={Boolean(inlineSaveBusy)}
          onChange={onInlineDraftChange}
          onSave={onSaveInlineDraft}
          onCancel={() => onInlineDraftChange(null)}
        />
      )
      : null
  )

  const table = (
    <div className={cn('overflow-x-auto', insertionTargets && onInsertTrade && 'pb-3')}>
      <table className="w-full min-w-[840px] text-left text-xs">
        <thead className="bg-elevated/50 text-muted">
          <tr>
            <th className="w-7" />
            <th className="px-4 py-2 font-medium">交易日</th>
            {mode === 'byDate' ? (
              <th className="px-4 py-2 font-medium">标的 / 账户</th>
            ) : (
              <th className="px-3 py-2 font-medium">账户</th>
            )}
            <th className="px-3 py-2 font-medium">方向</th>
            <th className="px-3 py-2 text-right font-medium">成交价 × 数量</th>
            <th className="px-3 py-2 text-right font-medium">费用 / 税费</th>
            <th className="px-3 py-2 font-medium">备注</th>
            <th className="px-4 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/70">
          {sortable ? items.map((trade, index) => (
            <Fragment key={trade.id}>
              <SortableTradeRow
                id={trade.id}
                busy={Boolean(reorderBusy || interactionDisabled)}
                trade={trade}
                insertionTarget={insertionTargets?.[index]}
                onInsertTrade={onInsertTrade}
                insertionDisabled={Boolean(reorderBusy || tradeEditBusy || interactionDisabled)}
              >
                {renderFirstCells(trade)}
                <TradeRowCells trade={trade} onDelete={onDelete} onEditCost={onEditCost} onUpdateExecution={onUpdateExecution} tradeEditBusy={tradeEditBusy} interactionDisabled={interactionDisabled} />
              </SortableTradeRow>
              {renderInlineDraft(trade)}
            </Fragment>
          )) : items.map((trade, index) => (
            <Fragment key={trade.id}>
              <tr className="group/trade hover:bg-elevated/20">
                <td className="relative w-7 pl-2 pr-0">
                  {insertionTargets?.[index] && onInsertTrade && (
                    <TradeInsertionButton trade={trade} target={insertionTargets[index]} onInsert={onInsertTrade} disabled={Boolean(reorderBusy || tradeEditBusy || interactionDisabled)} />
                  )}
                </td>
                {renderFirstCells(trade)}
                <TradeRowCells trade={trade} onDelete={onDelete} onEditCost={onEditCost} onUpdateExecution={onUpdateExecution} tradeEditBusy={tradeEditBusy} interactionDisabled={interactionDisabled} />
              </tr>
              {renderInlineDraft(trade)}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )

  if (!sortable) return table
  return (
    <DndContext sensors={dndSensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={items.map(trade => trade.id)} strategy={verticalListSortingStrategy}>
        {table}
      </SortableContext>
    </DndContext>
  )
}

function InlineTradeDraftRow({ draft, mode, accountName, busy, onChange, onSave, onCancel }: { draft: InlineTradeDraft; mode: 'byDate' | 'byStock'; accountName: string; busy: boolean; onChange: (draft: InlineTradeDraft) => void; onSave: () => void; onCancel: () => void }) {
  const canSave = buildInlineTradeCreatePayload(draft) !== null
  const saveOnEnter = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && canSave && !busy) onSave()
  }
  const cancelOnEscape = (event: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key !== 'Escape' || busy) return
    event.stopPropagation()
    onCancel()
  }
  const firstCells = (
    <>
      <td className="px-4 py-2.5 font-mono">{draft.tradeDate}</td>
      {mode === 'byDate' ? (
        <td className="px-3 py-2.5">
          <div>{draft.symbol}</div>
          <div className="font-mono text-[10px] text-muted">{draft.symbol} · {accountName}</div>
        </td>
      ) : (
        <td className="px-3 py-2.5 text-muted">{accountName}</td>
      )}
    </>
  )

  return (
    <tr data-inline-trade-draft="true" onKeyDown={cancelOnEscape} className="bg-accent/[0.07]">
      <td className="w-7 border-l-[3px] border-l-accent px-0" />
      {firstCells}
      <td className="px-3 py-2.5">
        <select
          aria-label="新交易方向"
          value={draft.side}
          disabled={busy}
          onChange={event => onChange({ ...draft, side: event.target.value as 'buy' | 'sell' })}
          className={`${INPUT_CLASS} !w-[76px] !py-1.5`}
        >
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
      </td>
      <td className="px-3 py-2.5">
        <div className="flex items-center justify-end gap-1 font-mono">
          <span className="text-muted">¥</span>
          <input
            aria-label="新交易成交价"
            type="number"
            step="0.001"
            min="0"
            value={draft.price}
            autoFocus
            disabled={busy}
            onChange={event => onChange({ ...draft, price: event.target.value })}
            onKeyDown={saveOnEnter}
            className={`${INPUT_CLASS} !w-24 !py-1.5 text-right font-mono`}
          />
          <span className="text-muted">×</span>
          <input
            aria-label="新交易数量"
            type="number"
            step="any"
            min="0"
            value={draft.quantity}
            disabled={busy}
            onChange={event => onChange({ ...draft, quantity: event.target.value })}
            onKeyDown={saveOnEnter}
            className={`${INPUT_CLASS} !w-20 !py-1.5 text-right font-mono`}
          />
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right text-[11px] text-muted">保存时自动计算</td>
      <td className="px-3 py-2.5 text-secondary">未保存</td>
      <td className="px-4 py-2.5 text-right">
        <div className="flex items-center justify-end gap-1.5">
          <button
            type="button"
            onClick={onSave}
            disabled={busy || !canSave}
            className="inline-flex h-7 items-center gap-1 rounded-btn bg-accent px-2.5 text-[11px] font-medium text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
            title={canSave ? '保存新增明细' : '请填写正数量及有效成交价'}
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}保存
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="h-7 rounded-btn px-2 text-[11px] text-muted hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            取消
          </button>
        </div>
      </td>
    </tr>
  )
}

function TradeInsertionButton({ trade, target, onInsert, disabled }: { trade: PortfolioTrade; target: TradeInsertionTarget; onInsert: (trade: PortfolioTrade, target: TradeInsertionTarget) => void; disabled: boolean }) {
  return (
    <span className="pointer-events-none absolute -bottom-3 left-0 z-20 h-6 w-7">
      <button
        type="button"
        onClick={() => onInsert(trade, target)}
        disabled={disabled}
        aria-label={`在 ${target.tradeDate} 的此处插入一条交易明细`}
        title="在此处插入一条明细"
        className="pointer-events-auto absolute left-1/2 top-1/2 grid h-7 w-7 -translate-x-1/2 -translate-y-1/2 scale-75 place-items-center rounded-full border-2 border-surface bg-accent text-white opacity-0 shadow-lg shadow-accent/25 transition-all group-hover/trade:scale-100 group-hover/trade:opacity-100 group-focus-within/trade:scale-100 group-focus-within/trade:opacity-100 focus:scale-100 focus:opacity-100 hover:bg-accent/90 disabled:pointer-events-none disabled:opacity-0"
      >
        <Plus className="h-4 w-4" strokeWidth={2.5} />
      </button>
    </span>
  )
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="grid min-h-56 place-items-center px-4 text-center"><div><BriefcaseBusiness className="mx-auto h-7 w-7 text-muted/60" /><div className="mt-3 text-sm font-medium">{title}</div><p className="mt-1 max-w-md text-xs leading-relaxed text-muted">{detail}</p></div></div>
}

function TradeDialog({ accounts, draft, busy, tradingDates, earliestTradingDate, latestTradingDate, onChange, onClose, onSave }: { accounts: PortfolioAccount[]; draft: TradeDraft; busy: boolean; tradingDates: readonly string[]; earliestTradingDate?: string | null; latestTradingDate?: string | null; onChange: (draft: TradeDraft) => void; onClose: () => void; onSave: () => void }) {
  const [estimateBusy, setEstimateBusy] = useState(false)
  const mountedRef = useRef(true)
  const latestDraftRef = useRef(draft)
  latestDraftRef.current = draft
  const initialFocusRef = useRef<HTMLSelectElement>(null)
  const quantity = Number(draft.quantity)
  const price = Number(draft.price)
  const canEstimate = Boolean(draft.symbol.trim())
    && draft.quantity.trim() !== '' && Number.isFinite(quantity) && quantity > 0
    && draft.price.trim() !== '' && Number.isFinite(price) && price >= 0
  const closeIfIdle = () => { if (!busy) onClose() }

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  function updateDraftContext(next: Partial<Pick<TradeDraft, 'accountId' | 'symbol' | 'tradeDate'>>) {
    const contextChanged = (
      (next.accountId !== undefined && next.accountId !== draft.accountId)
      || (next.symbol !== undefined && next.symbol !== draft.symbol)
      || (next.tradeDate !== undefined && next.tradeDate !== draft.tradeDate)
    )
    onChange({
      ...draft,
      ...next,
      ...(contextChanged ? { insertBeforeTradeId: undefined } : {}),
    })
  }

  async function estimate() {
    if (!canEstimate || busy || estimateBusy) return
    setEstimateBusy(true)
    try {
      const requested: TradeEstimateContext = {
        symbol: draft.symbol.trim().toUpperCase(),
        side: draft.side,
        quantity,
        price,
      }
      const result = await api.portfolioTradeEstimate(requested)
      if (!mountedRef.current) return
      const current = latestDraftRef.current
      const next = mergeTradeEstimateIfCurrent(current, requested, result)
      if (next !== current) onChange(next)
    } catch {
      // request 内已弹错误 toast
    } finally {
      if (mountedRef.current) setEstimateBusy(false)
    }
  }

  return (
    <Modal
      onClose={closeIfIdle}
      labelledBy="trade-dialog-title"
      initialFocusRef={initialFocusRef}
      panelClassName="w-full max-w-lg rounded-card border border-border bg-surface shadow-2xl"
      overlayClassName="fixed inset-0 z-50 grid place-items-center bg-black/55 p-4"
      closeOnBackdrop={!busy}
    >
        <div className="flex items-center justify-between border-b border-border px-4 py-3"><div><h2 id="trade-dialog-title" className="text-sm font-medium">记录交易</h2><p className="mt-0.5 text-[11px] text-muted">持仓将由全部买卖交易按日期和 FIFO 成本法自动汇总</p></div><button onClick={closeIfIdle} disabled={busy} className="rounded-btn p-1 text-muted hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40" title={busy ? '保存中，暂不可关闭' : '关闭'}><X className="h-4 w-4" /></button></div>
        <div className="space-y-3 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="账户" htmlFor="trade-account"><select ref={initialFocusRef} id="trade-account" value={draft.accountId} onChange={event => updateDraftContext({ accountId: event.target.value })} className={INPUT_CLASS}>{accounts.map(account => <option key={account.id} value={account.id}>{account.name}</option>)}</select></Field>
            <Field label="方向" htmlFor="trade-side"><select id="trade-side" value={draft.side} onChange={event => onChange({ ...draft, side: event.target.value as 'buy' | 'sell' })} className={INPUT_CLASS}><option value="buy">买入</option><option value="sell">卖出</option></select></Field>
          </div>
          <Field label="证券代码" hint="支持输入代码或名称联想" htmlFor="trade-symbol">
            <InstrumentSearchInput inputId="trade-symbol" value={draft.symbol} onValueChange={value => updateDraftContext({ symbol: value.toUpperCase() })} onSelect={result => updateDraftContext({ symbol: result.symbol })} assetTypes="stock,etf" placeholder="输入代码或名称，如 600519 / 茅台" inputClassName={`${INPUT_CLASS} font-mono`} menuClassName="left-0 right-0" emptyText="未找到匹配的股票或 ETF" />
          </Field>
          <Field label="交易日" hint={draft.insertBeforeTradeId ? '已按所选位置预填；改日后追加到当日' : '仅可选择本地已有行情的交易日'} htmlFor="trade-date">
            <DatePicker buttonId="trade-date" value={draft.tradeDate} onChange={tradeDate => updateDraftContext({ tradeDate })} min={earliestTradingDate ?? undefined} max={latestTradingDate ?? localDateIso()} availableDates={tradingDates.length > 0 ? tradingDates : undefined} align="left" className="w-full" buttonClassName="h-9 w-full justify-start rounded-btn px-2.5 font-mono" />
          </Field>
          <div className="grid grid-cols-2 gap-3"><Field label="成交价格" htmlFor="trade-price"><input id="trade-price" type="number" min="0" step="any" value={draft.price} onChange={event => onChange({ ...draft, price: event.target.value })} className={`${INPUT_CLASS} font-mono`} /></Field><Field label="成交数量" htmlFor="trade-quantity"><input id="trade-quantity" type="number" min="0" step="any" value={draft.quantity} onChange={event => onChange({ ...draft, quantity: event.target.value })} className={`${INPUT_CLASS} font-mono`} /></Field></div>
          <div className="flex items-center justify-between rounded-btn border border-border bg-elevated/40 px-3 py-2">
            <span className="text-[11px] text-muted">按「设置 → 交易费率」估算本次费用/税费</span>
            <button onClick={estimate} disabled={!canEstimate || busy || estimateBusy} className="flex h-7 shrink-0 items-center gap-1 rounded-btn border border-accent/25 bg-accent/5 px-2.5 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-40">
              {estimateBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <CircleDollarSign className="h-3 w-3" />}
              估算
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3"><Field label="手续费" hint="留空按费率自动估算" htmlFor="trade-fee"><input id="trade-fee" type="number" min="0" step="any" value={draft.fee} onChange={event => onChange({ ...draft, fee: event.target.value })} placeholder="留空自动估算" className={`${INPUT_CLASS} font-mono`} /></Field><Field label="税费" hint="留空按费率自动估算" htmlFor="trade-tax"><input id="trade-tax" type="number" min="0" step="any" value={draft.tax} onChange={event => onChange({ ...draft, tax: event.target.value })} placeholder="留空自动估算" className={`${INPUT_CLASS} font-mono`} /></Field></div>
          <Field label="备注" hint="可选；最近一笔非空备注会展示在持仓上" htmlFor="trade-note"><textarea id="trade-note" value={draft.note} onChange={event => onChange({ ...draft, note: event.target.value })} rows={3} className={`${INPUT_CLASS} resize-none`} /></Field>
          {draft.side === 'sell' && <div className="rounded-btn border border-warning/25 bg-warning/5 px-3 py-2 text-[11px] leading-relaxed text-warning">卖出数量不能超过该交易日的可用持仓；补录历史卖出也会校验它之后的所有交易。</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-4 py-3"><button onClick={closeIfIdle} disabled={busy} className="h-8 rounded-btn px-3 text-xs text-secondary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40">取消</button><button onClick={onSave} disabled={busy || estimateBusy} className="flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50">{busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}保存交易</button></div>
    </Modal>
  )
}

function Field({ label, hint, htmlFor, children }: { label: string; hint?: string; htmlFor: string; children: ReactNode }) {
  return <div className="block space-y-1.5"><label htmlFor={htmlFor} className="flex items-center justify-between text-xs text-secondary"><span>{label}</span>{hint && <span className="text-[10px] text-muted">{hint}</span>}</label>{children}</div>
}
