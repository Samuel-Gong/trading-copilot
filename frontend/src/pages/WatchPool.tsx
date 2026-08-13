import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle,
  ArrowRight,
  BellPlus,
  Binoculars,
  CheckCircle2,
  Clock3,
  Loader2,
  RadioTower,
  Search,
  Trash2,
  X,
} from 'lucide-react'

import { InstrumentSearchInput } from '@/components/InstrumentSearchInput'
import { PageHeader } from '@/components/PageHeader'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { toast } from '@/components/Toast'
import { api, type MonitorRule, type PortfolioWatchItem } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { MONITOR_PRICE_CROSS_SIGNAL_OPTIONS, cnSignal } from '@/lib/signals'

const SEARCH_INPUT_CLASS = 'h-10 w-full rounded-btn border border-border bg-base pr-3 text-sm text-foreground outline-none transition-colors focus:border-accent/60 disabled:opacity-60'

const RULE_TYPE_LABEL: Record<MonitorRule['type'], string> = {
  signal: '信号',
  price: '价格',
  market: '异动',
  strategy: '策略',
  ladder: '封单',
}

const RULE_TYPE_STYLE: Record<MonitorRule['type'], string> = {
  signal: 'border-accent/20 bg-accent/[0.06] text-accent',
  price: 'border-emerald-400/20 bg-emerald-400/[0.06] text-emerald-400',
  market: 'border-purple-400/20 bg-purple-400/[0.06] text-purple-400',
  strategy: 'border-amber-400/20 bg-amber-400/[0.06] text-amber-400',
  ladder: 'border-warning/20 bg-warning/[0.06] text-warning',
}

const STRATEGY_EVENT_LABEL: Record<string, string> = {
  buy_signal: '买入信号',
  sell_signal: '卖出信号',
  pool_entry: '进入选股结果',
  pool_exit: '移出选股结果',
}

function addedAtLabel(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

function rulesForSymbol(rules: MonitorRule[], symbol: string) {
  return rules.filter(rule => rule.scope === 'symbols' && rule.symbols.includes(symbol))
}

function conditionSummary(rule: MonitorRule, symbol: string) {
  if (rule.type === 'strategy') {
    const events = (rule.notify_events ?? []).map(event => STRATEGY_EVENT_LABEL[event] ?? event)
    return events.length > 0 ? events.join(' / ') : '策略结果变化时触发'
  }
  if (rule.type === 'ladder' && rule.threshold != null) {
    return `${rule.metric === 'sealed_amount' ? '封单金额' : '封单数量'} <= ${rule.threshold}`
  }
  if (rule.conditions.length === 0) return '尚未配置触发条件'

  const conditions = rule.conditions.slice(0, 3).map(condition => {
    const field = cnSignal(condition.field)
    if (condition.op !== 'truth') return `${field} ${condition.op} ${condition.value ?? '—'}`
    const priceLevel = rule.intraday_price_levels?.[symbol]
    return priceLevel && MONITOR_PRICE_CROSS_SIGNAL_OPTIONS.includes(condition.field)
      ? `${field} ${priceLevel}`
      : field
  })
  const separator = rule.logic === 'and' ? ' 且 ' : ' 或 '
  const remaining = rule.conditions.length - conditions.length
  return `${conditions.join(separator)}${remaining > 0 ? ` · 另 ${remaining} 项` : ''}`
}

function displayRuleName(rule: MonitorRule, symbol: string) {
  const name = rule.name || '未命名规则'
  const generatedSuffix = ` · ${symbol}`
  return name.endsWith(generatedSuffix) ? name.slice(0, -generatedSuffix.length) : name
}

export function WatchPool() {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [monitorTarget, setMonitorTarget] = useState<PortfolioWatchItem | null>(null)
  const [confirmDeleteRuleId, setConfirmDeleteRuleId] = useState<string | null>(null)

  const poolQuery = useQuery({
    queryKey: QK.portfolioWatchPool,
    queryFn: api.portfolioWatchPool,
  })
  const monitorRulesQuery = useQuery({
    queryKey: QK.monitorRules,
    queryFn: api.monitorRulesList,
  })

  const items = poolQuery.data?.items ?? []
  const rules = monitorRulesQuery.data?.rules ?? []
  const monitorRulesBySymbol = useMemo(() => {
    const result: Record<string, MonitorRule[]> = {}
    for (const item of items) result[item.symbol] = rulesForSymbol(rules, item.symbol)
    return result
  }, [items, rules])

  const addMutation = useMutation({
    mutationFn: (symbol: string) => api.portfolioWatchPoolAdd(symbol),
    onSuccess: async item => {
      setQuery('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: QK.portfolioWatchPool }),
        queryClient.invalidateQueries({ queryKey: QK.watchlist }),
      ])
      toast(`${item.name} 已加入观察池`, 'success')
    },
  })

  const removeMutation = useMutation({
    mutationFn: (symbol: string) => api.portfolioWatchPoolRemove(symbol),
    onSuccess: async (_result, symbol) => {
      await queryClient.invalidateQueries({ queryKey: QK.portfolioWatchPool })
      toast(`${symbol} 已移出观察池`, 'success')
    },
  })

  const deleteRuleMutation = useMutation({
    mutationFn: ({ id }: { id: string; name: string }) => api.monitorRuleDelete(id),
    onSuccess: async (_result, rule) => {
      setConfirmDeleteRuleId(null)
      await queryClient.invalidateQueries({ queryKey: QK.monitorRules })
      toast(`监控「${rule.name || '未命名规则'}」已删除`, 'success')
    },
  })

  return (
    <div className="min-h-full bg-base">
      <PageHeader
        title="观察池"
        subtitle="维护尚未持仓、可能建仓的标的，并直接添加监控规则"
        right={(
          <Link
            to="/monitor"
            className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-3 text-xs text-secondary transition-colors hover:border-accent/30 hover:text-accent"
          >
            <RadioTower className="h-3.5 w-3.5" />
            监控中心
          </Link>
        )}
      />

      <main className="mx-auto max-w-[1280px] space-y-4 p-4 md:p-5">
        <section className="overflow-visible rounded-card border border-border bg-surface">
          <div className="grid gap-4 p-4 lg:grid-cols-[minmax(280px,440px)_1fr] lg:items-center">
            <div>
              <label htmlFor="watch-pool-search" className="mb-2 block text-xs font-medium text-secondary">
                添加观察标的
              </label>
              <InstrumentSearchInput
                inputId="watch-pool-search"
                value={query}
                onValueChange={setQuery}
                onSelect={result => addMutation.mutate(result.symbol)}
                assetTypes="stock,etf"
                placeholder="输入股票代码或名称，如 600519 / 茅台"
                inputClassName={SEARCH_INPUT_CLASS}
                menuClassName="left-0 right-0"
                disabled={addMutation.isPending}
                emptyText="未找到匹配的股票或 ETF"
              />
            </div>

            <div className="rounded-card border border-accent/15 bg-accent/[0.04] px-4 py-3">
              <div className="flex items-start gap-3">
                <Binoculars className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                <div>
                  <div className="text-xs font-medium text-foreground">观察池是建仓前的研究状态</div>
                  <p className="mt-1 text-[11px] leading-5 text-muted">
                    新增标的会同时加入自选，便于实时行情和监控消费；任一账户建仓后，该标的会自动移出观察池，但不会删除自选或已经配置的监控规则。
                  </p>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <h2 className="text-sm font-medium">正在观察</h2>
              <p className="mt-0.5 text-[11px] text-muted">只保存当前研究范围，不记录数量、成本或预期收益</p>
            </div>
            <span className="rounded-full bg-elevated px-2.5 py-1 font-mono text-[11px] text-secondary">
              {items.length} 只
            </span>
          </div>

          {poolQuery.isLoading ? (
            <div className="grid min-h-64 place-items-center" role="status">
              <Loader2 className="h-5 w-5 animate-spin text-muted" />
            </div>
          ) : poolQuery.isError ? (
            <div className="grid min-h-64 place-items-center px-4 text-center">
              <div>
                <div className="text-sm font-medium text-danger">观察池加载失败</div>
                <button
                  type="button"
                  onClick={() => poolQuery.refetch()}
                  className="mt-3 rounded-btn border border-border px-3 py-1.5 text-xs text-secondary hover:text-foreground"
                >
                  重新加载
                </button>
              </div>
            </div>
          ) : items.length === 0 ? (
            <div className="grid min-h-64 place-items-center px-4 text-center">
              <div>
                <div className="mx-auto grid h-12 w-12 place-items-center rounded-full border border-border bg-elevated/50">
                  <Search className="h-5 w-5 text-muted" />
                </div>
                <div className="mt-3 text-sm font-medium">还没有观察标的</div>
                <p className="mt-1 text-xs text-muted">从上方搜索股票或 ETF，选中后即可加入。</p>
              </div>
            </div>
          ) : (
            <div className="divide-y divide-border/70">
              {items.map(item => {
                const symbolRules = monitorRulesBySymbol[item.symbol] ?? []
                const monitorCount = symbolRules.length
                return (
                  <article
                    key={item.symbol}
                    className="grid gap-4 px-4 py-4 transition-colors hover:bg-elevated/20 lg:grid-cols-[minmax(180px,1fr)_minmax(360px,1.5fr)_auto] lg:items-start"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="truncate text-sm font-medium">{item.name || item.symbol}</h3>
                        <span className="shrink-0 rounded border border-accent/20 bg-accent/5 px-1.5 py-0.5 text-[9px] font-medium uppercase text-accent">
                          {item.asset_type === 'etf' ? 'ETF' : '股票'}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center gap-2 font-mono text-[11px] text-muted">
                        <span>{item.symbol}</span>
                        <span aria-hidden="true">·</span>
                        <span className="inline-flex items-center gap-1 font-sans">
                          <Clock3 className="h-3 w-3" />{addedAtLabel(item.added_at)} 加入
                        </span>
                      </div>
                    </div>

                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2 overflow-x-auto pb-1 lg:pb-0" aria-label={`${item.name} 状态轨道`}>
                        <StatusNode active icon={Binoculars} label="观察中" />
                        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-border" />
                        <StatusNode
                          active={!monitorRulesQuery.isError && monitorCount > 0}
                          icon={monitorRulesQuery.isError ? AlertTriangle : monitorCount > 0 ? CheckCircle2 : BellPlus}
                          label={monitorRulesQuery.isLoading
                            ? '读取监控'
                            : monitorRulesQuery.isError
                              ? '监控加载失败'
                              : monitorCount > 0 ? `${monitorCount} 条监控` : '待加监控'}
                        />
                        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-border" />
                        <StatusNode icon={X} label="建仓后移出" />
                      </div>

                      {monitorRulesQuery.isLoading ? (
                        <div className="mt-2 h-12 animate-pulse rounded-md border border-border/50 bg-elevated/25" aria-label="监控规则加载中" />
                      ) : monitorRulesQuery.isError ? (
                        <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-danger/20 bg-danger/[0.04] px-2.5 py-2 text-[10px] text-danger">
                          <span>监控信息加载失败</span>
                          <button
                            type="button"
                            onClick={() => monitorRulesQuery.refetch()}
                            className="shrink-0 rounded border border-danger/25 px-1.5 py-0.5 transition-colors hover:bg-danger/10"
                          >
                            重试
                          </button>
                        </div>
                      ) : symbolRules.length > 0 ? (
                        <div className="mt-2 space-y-1.5" aria-label={`${item.name} 的监控规则`}>
                          {symbolRules.map(rule => (
                            <WatchPoolMonitorRule
                              key={rule.id}
                              rule={rule}
                              symbol={item.symbol}
                              confirming={confirmDeleteRuleId === rule.id}
                              deleting={deleteRuleMutation.isPending && deleteRuleMutation.variables?.id === rule.id}
                              deleteDisabled={deleteRuleMutation.isPending}
                              onRequestDelete={() => setConfirmDeleteRuleId(rule.id)}
                              onCancelDelete={() => setConfirmDeleteRuleId(null)}
                              onConfirmDelete={() => deleteRuleMutation.mutate({ id: rule.id, name: rule.name })}
                            />
                          ))}
                        </div>
                      ) : null}
                    </div>

                    <div className="flex items-center justify-end gap-2 lg:pt-0.5">
                      <button
                        type="button"
                        onClick={() => setMonitorTarget(item)}
                        className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-accent/25 bg-accent/5 px-3 text-xs text-accent transition-colors hover:bg-accent/10"
                      >
                        <BellPlus className="h-3.5 w-3.5" />
                        {monitorCount > 0 ? '再加规则' : '添加监控'}
                      </button>
                      <button
                        type="button"
                        onClick={() => removeMutation.mutate(item.symbol)}
                        disabled={removeMutation.isPending}
                        title="移出观察池；自选和监控规则保留"
                        className="grid h-8 w-8 place-items-center rounded-btn border border-border text-muted transition-colors hover:border-danger/30 hover:bg-danger/5 hover:text-danger disabled:opacity-40"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      </main>

      <MonitorDialog target={monitorTarget} onClose={() => setMonitorTarget(null)} />
    </div>
  )
}

function StatusNode({ active = false, icon: Icon, label }: {
  active?: boolean
  icon: typeof Binoculars
  label: string
}) {
  return (
    <span className={cn(
      'inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-[10px] font-medium',
      active
        ? 'border-accent/25 bg-accent/[0.07] text-accent'
        : 'border-border bg-elevated/30 text-muted',
    )}>
      <Icon className="h-3 w-3" />
      {label}
    </span>
  )
}

function WatchPoolMonitorRule({
  rule,
  symbol,
  confirming,
  deleting,
  deleteDisabled,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  rule: MonitorRule
  symbol: string
  confirming: boolean
  deleting: boolean
  deleteDisabled: boolean
  onRequestDelete: () => void
  onCancelDelete: () => void
  onConfirmDelete: () => void
}) {
  const name = displayRuleName(rule, symbol)
  const summary = conditionSummary(rule, symbol)
  return (
    <div className={cn(
      'relative overflow-hidden rounded-md border border-border/55 bg-base/45 py-2 pl-3 pr-2',
      !rule.enabled && 'opacity-65',
    )}>
      <div className={cn('absolute inset-y-0 left-0 w-0.5', rule.enabled ? 'bg-accent/55' : 'bg-border')} />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className={cn('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-medium', RULE_TYPE_STYLE[rule.type])}>
              {RULE_TYPE_LABEL[rule.type]}
            </span>
            <span className="min-w-0 truncate text-[11px] font-medium text-foreground" title={name}>{name}</span>
            {!rule.enabled && <span className="shrink-0 text-[9px] text-muted">已停用</span>}
            {rule.symbols.length > 1 && (
              <span className="shrink-0 rounded bg-elevated px-1.5 py-0.5 text-[9px] text-muted" title="删除会影响这条规则覆盖的全部标的">
                覆盖 {rule.symbols.length} 只
              </span>
            )}
          </div>
          <div className="mt-1 break-words font-mono text-[10px] leading-4 text-secondary">
            {summary}
          </div>
        </div>

        {confirming ? (
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={onCancelDelete}
              disabled={deleting}
              className="h-7 rounded border border-border px-2 text-[10px] text-muted transition-colors hover:text-foreground disabled:opacity-40"
            >
              取消
            </button>
            <button
              type="button"
              onClick={onConfirmDelete}
              disabled={deleting}
              className="inline-flex h-7 items-center gap-1 rounded border border-danger/30 bg-danger/[0.07] px-2 text-[10px] font-medium text-danger transition-colors hover:bg-danger/10 disabled:opacity-40"
            >
              {deleting && <Loader2 className="h-3 w-3 animate-spin" />}
              确认删除
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={onRequestDelete}
            disabled={deleteDisabled}
            aria-label={`删除监控规则 ${name}：${summary}`}
            title={rule.symbols.length > 1 ? `删除整条规则，将影响 ${rule.symbols.length} 只标的` : '删除这条监控规则'}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded border border-transparent px-1.5 text-[10px] text-muted transition-colors hover:border-danger/20 hover:bg-danger/[0.06] hover:text-danger disabled:opacity-40"
          >
            <Trash2 className="h-3 w-3" />
            删除
          </button>
        )}
      </div>
    </div>
  )
}

function MonitorDialog({ target, onClose }: {
  target: PortfolioWatchItem | null
  onClose: () => void
}) {
  return (
    <AnimatePresence>
      {target && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-black/45 p-4 backdrop-blur-sm"
          onClick={onClose}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            className="mt-8 w-full max-w-2xl"
            onClick={event => event.stopPropagation()}
          >
            <RuleEditor
              rule={null}
              simple
              defaultThresholdCondition={{ field: 'last_price', op: '<=' }}
              preset={{
                scope: 'symbols',
                symbols: [target.symbol],
                asset_type: target.asset_type,
                type: 'signal',
                logic: 'or',
                cooldown_seconds: 1200,
              }}
              onClose={onClose}
              onSaved={onClose}
            />
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
