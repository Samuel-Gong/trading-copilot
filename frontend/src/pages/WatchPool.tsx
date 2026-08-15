import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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

import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { api, type MonitorRule } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { WatchPoolAddSection } from '@/pages/watch-pool/WatchPoolAddSection'
import {
  WatchPoolMonitorDialog,
  type WatchPoolMonitorDialogState,
} from '@/pages/watch-pool/WatchPoolMonitorDialog'
import { WatchPoolMonitorRule, WatchPoolStatusNode } from '@/pages/watch-pool/WatchPoolMonitorRule'

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

export function WatchPool() {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [monitorDialog, setMonitorDialog] = useState<WatchPoolMonitorDialogState | null>(null)
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
        <WatchPoolAddSection
          query={query}
          disabled={addMutation.isPending}
          onQueryChange={setQuery}
          onSelect={symbol => addMutation.mutate(symbol)}
        />

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
                        <WatchPoolStatusNode active icon={Binoculars} label="观察中" />
                        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-border" />
                        <WatchPoolStatusNode
                          active={!monitorRulesQuery.isError && monitorCount > 0}
                          icon={monitorRulesQuery.isError ? AlertTriangle : monitorCount > 0 ? CheckCircle2 : BellPlus}
                          label={monitorRulesQuery.isLoading
                            ? '读取监控'
                            : monitorRulesQuery.isError
                              ? '监控加载失败'
                              : monitorCount > 0 ? `${monitorCount} 条监控` : '待加监控'}
                        />
                        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-border" />
                        <WatchPoolStatusNode icon={X} label="建仓后移出" />
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
                              onEdit={() => setMonitorDialog({ target: item, rule })}
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
                        onClick={() => setMonitorDialog({ target: item, rule: null })}
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

      <WatchPoolMonitorDialog state={monitorDialog} onClose={() => setMonitorDialog(null)} />
    </div>
  )
}
