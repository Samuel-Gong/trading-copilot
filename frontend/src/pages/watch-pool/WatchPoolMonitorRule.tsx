import { Loader2, Settings2, Trash2 } from 'lucide-react'

import type { MonitorRule } from '@/lib/api'
import { cn } from '@/lib/cn'
import { MONITOR_PRICE_CROSS_SIGNAL_OPTIONS, cnSignal } from '@/lib/signals'

const RULE_TYPE_LABEL: Record<MonitorRule['type'], string> = {
  signal: '信号',
  price: '价格',
  market: '异动',
  strategy: '策略',
  ladder: '封单',
  sector: '板块',
  abnormal: '异动边缘',
}

const RULE_TYPE_STYLE: Record<MonitorRule['type'], string> = {
  signal: 'border-accent/20 bg-accent/[0.06] text-accent',
  price: 'border-emerald-400/20 bg-emerald-400/[0.06] text-emerald-400',
  market: 'border-purple-400/20 bg-purple-400/[0.06] text-purple-400',
  strategy: 'border-amber-400/20 bg-amber-400/[0.06] text-amber-400',
  ladder: 'border-warning/20 bg-warning/[0.06] text-warning',
  sector: 'border-cyan-500/20 bg-cyan-500/[0.06] text-cyan-500',
  abnormal: 'border-orange-500/20 bg-orange-500/[0.06] text-orange-500',
}

const STRATEGY_EVENT_LABEL: Readonly<Record<string, string>> = {
  buy_signal: '买入信号',
  sell_signal: '卖出信号',
  pool_entry: '进入选股结果',
  pool_exit: '移出选股结果',
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

type StatusNodeProps = {
  readonly active?: boolean
  readonly icon: typeof Settings2
  readonly label: string
}

export function WatchPoolStatusNode({ active = false, icon: Icon, label }: StatusNodeProps) {
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

type MonitorRuleProps = {
  readonly rule: MonitorRule
  readonly symbol: string
  readonly confirming: boolean
  readonly deleting: boolean
  readonly deleteDisabled: boolean
  readonly onEdit: () => void
  readonly onRequestDelete: () => void
  readonly onCancelDelete: () => void
  readonly onConfirmDelete: () => void
}

export function WatchPoolMonitorRule({
  rule,
  symbol,
  confirming,
  deleting,
  deleteDisabled,
  onEdit,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
}: MonitorRuleProps) {
  const name = displayRuleName(rule, symbol)
  const summary = conditionSummary(rule, symbol)
  const editSupported = rule.type !== 'ladder'
  const editTitle = !editSupported
    ? '封单规则需在涨跌停梯队页面维护，观察池暂不支持编辑'
    : rule.symbols.length > 1
      ? `编辑整条规则，将影响 ${rule.symbols.length} 只标的`
      : '编辑这条监控规则'

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
              <span className="shrink-0 rounded bg-elevated px-1.5 py-0.5 text-[9px] text-muted" title="修改或删除会影响这条规则覆盖的全部标的">
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
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={onEdit}
              disabled={!editSupported}
              aria-label={`编辑监控规则 ${name}：${summary}`}
              title={editTitle}
              className="inline-flex h-7 items-center gap-1 rounded border border-transparent px-1.5 text-[10px] text-muted transition-colors hover:border-accent/20 hover:bg-accent/[0.06] hover:text-accent disabled:cursor-not-allowed disabled:hover:border-transparent disabled:hover:bg-transparent disabled:hover:text-muted"
            >
              <Settings2 className="h-3 w-3" />
              {editSupported ? '编辑' : '暂不可编辑'}
            </button>
            <button
              type="button"
              onClick={onRequestDelete}
              disabled={deleteDisabled}
              aria-label={`删除监控规则 ${name}：${summary}`}
              title={rule.symbols.length > 1 ? `删除整条规则，将影响 ${rule.symbols.length} 只标的` : '删除这条监控规则'}
              className="inline-flex h-7 items-center gap-1 rounded border border-transparent px-1.5 text-[10px] text-muted transition-colors hover:border-danger/20 hover:bg-danger/[0.06] hover:text-danger disabled:opacity-40"
            >
              <Trash2 className="h-3 w-3" />
              删除
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
