import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDollarSign,
  ClipboardCheck,
  Clock3,
  FileText,
  History,
  Layers3,
  LineChart,
  Loader2,
  Play,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Square,
  X,
} from 'lucide-react'

import { DailyReviewAnalysisGraphView } from '@/components/daily-review/DailyReviewAnalysisGraph'
import { DatePicker } from '@/components/DatePicker'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toast } from '@/components/Toast'
import {
  api,
  DAILY_REVIEW_GRAPH_SCHEMA_VERSION,
  type DailyReviewCandidate,
  type DailyReviewGraphNode,
  type DailyReviewItemStatus,
  type DailyReviewPosition,
  type DailyReviewRoutine,
  type DailyReviewRoutineSummary,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { useTradingDates } from '@/lib/useSharedQueries'
import { useStrategyPool } from '@/lib/useStrategyPool'

function formatMoney(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

function formatRatio(value: number | null | undefined) {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

const ITEM_STATUS: Record<DailyReviewItemStatus, { label: string; className: string }> = {
  pending: { label: '等待中', className: 'text-muted border-border bg-elevated' },
  running: { label: '分析中', className: 'text-accent border-accent/30 bg-accent/10' },
  completed: { label: '已完成', className: 'text-success border-success/30 bg-success/10' },
  failed: { label: '失败', className: 'text-danger border-danger/30 bg-danger/10' },
  interrupted: { label: '已中断', className: 'text-warning border-warning/30 bg-warning/10' },
  blocked: { label: '前置阻塞', className: 'text-warning border-warning/30 bg-warning/10' },
}

function StatusBadge({ status }: { status: DailyReviewItemStatus }) {
  const config = ITEM_STATUS[status]
  return (
    <span className={cn('inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium', config.className)}>
      {status === 'running' && <Loader2 className="mr-1 h-2.5 w-2.5 animate-spin" />}
      {config.label}
    </span>
  )
}

type ReviewStepId = 'market' | 'candidates' | 'positions'
type ReviewStepStatus = DailyReviewItemStatus | 'skipped'

type ReviewStep = {
  id: ReviewStepId
  number: string
  title: string
  description: string
  meta: string
  status: ReviewStepStatus
  icon: typeof FileText
}

const STEP_STATUS: Record<ReviewStepStatus, { label: string; className: string }> = {
  pending: { label: '等待开始', className: 'border-border bg-elevated text-muted' },
  running: { label: '进行中', className: 'border-accent/30 bg-accent/10 text-accent' },
  completed: { label: '已完成', className: 'border-success/30 bg-success/10 text-success' },
  failed: { label: '存在失败', className: 'border-danger/30 bg-danger/10 text-danger' },
  interrupted: { label: '已中断', className: 'border-warning/30 bg-warning/10 text-warning' },
  blocked: { label: '前置阻塞', className: 'border-warning/30 bg-warning/10 text-warning' },
  skipped: { label: '待补齐', className: 'border-warning/30 bg-warning/10 text-warning' },
}

function aggregateTargetStatus(items: Array<{ status: DailyReviewItemStatus }>): DailyReviewItemStatus {
  if (items.length === 0 || items.every(item => item.status === 'completed')) return 'completed'
  if (items.some(item => item.status === 'running')) return 'running'
  if (items.some(item => item.status === 'pending')) return 'pending'
  if (items.some(item => item.status === 'failed')) return 'failed'
  if (items.some(item => item.status === 'interrupted')) return 'interrupted'
  return 'blocked'
}

function candidateStepStatus(routine: DailyReviewRoutine): ReviewStepStatus {
  if (routine.strategy_screening.status !== 'completed') return routine.strategy_screening.status
  return aggregateTargetStatus(routine.candidates)
}

function positionStepStatus(routine: DailyReviewRoutine): ReviewStepStatus {
  if (
    routine.positions.length === 0
    && (routine.market_review.status !== 'completed' || candidateStepStatus(routine) !== 'completed')
  ) return 'blocked'
  return aggregateTargetStatus(routine.positions)
}

function initialStep(routine: DailyReviewRoutine): ReviewStepId {
  if (routine.market_review.status !== 'completed') return 'market'
  if (candidateStepStatus(routine) !== 'completed') return 'candidates'
  if (positionStepStatus(routine) !== 'completed') return 'positions'
  return 'market'
}

function StepStatusBadge({ status }: { status: ReviewStepStatus }) {
  const config = STEP_STATUS[status]
  return (
    <span className={cn('inline-flex items-center rounded-full border px-2 py-0.5 text-[9px] font-medium', config.className)}>
      {status === 'running' && <Loader2 className="mr-1 h-2.5 w-2.5 animate-spin" />}
      {config.label}
    </span>
  )
}

function StepMarker({ step, active }: { step: ReviewStep; active: boolean }) {
  const config = STEP_STATUS[step.status]
  return (
    <span className={cn(
      'relative z-10 grid h-9 w-9 shrink-0 place-items-center rounded-full border font-mono text-[11px] font-semibold ring-4 ring-surface transition-colors',
      config.className,
      active && 'border-accent bg-accent text-white',
    )}>
      {step.status === 'completed'
        ? <CheckCircle2 className="h-4 w-4" />
        : step.status === 'running'
          ? <Loader2 className="h-4 w-4 animate-spin" />
          : step.number}
    </span>
  )
}

function ReviewStepper({ steps, activeStep, onSelect }: {
  steps: ReviewStep[]
  activeStep: ReviewStepId
  onSelect: (step: ReviewStepId) => void
}) {
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface" aria-label="每日复盘流程">
      <div className="flex flex-wrap items-end justify-between gap-2 border-b border-border px-4 py-3">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-accent">Review Flow</div>
          <h2 className="mt-1 text-sm font-semibold text-foreground">每日复盘流程</h2>
        </div>
        <p className="text-[10px] text-muted">点击步骤，在下方查看该阶段的完整内容</p>
      </div>
      <div className="relative grid md:grid-cols-3" role="tablist" aria-label="每日复盘步骤">
        <div className="absolute left-[16.666%] right-[16.666%] top-[34px] hidden h-px bg-border md:block" aria-hidden="true" />
        {steps.map(step => {
          const active = activeStep === step.id
          const Icon = step.icon
          return (
            <button
              key={step.id}
              type="button"
              role="tab"
              id={`daily-review-step-tab-${step.id}`}
              aria-selected={active}
              aria-controls={`daily-review-step-${step.id}`}
              onClick={() => onSelect(step.id)}
              className={cn(
                'group relative flex items-start gap-3 border-b border-border px-4 py-4 text-left transition-colors last:border-b-0 focus-visible:z-20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent md:min-h-[118px] md:flex-col md:items-center md:border-b-0 md:px-5 md:text-center',
                active ? 'bg-accent/[0.07]' : 'hover:bg-elevated/45',
              )}
            >
              <StepMarker step={step} active={active} />
              <div className="min-w-0 flex-1 md:flex-none">
                <div className="flex flex-wrap items-center gap-2 md:justify-center">
                  <Icon className={cn('h-3.5 w-3.5', active ? 'text-accent' : 'text-muted')} />
                  <span className={cn('text-xs font-semibold', active ? 'text-foreground' : 'text-secondary')}>{step.title}</span>
                  <StepStatusBadge status={step.status} />
                </div>
                <p className="mt-1 line-clamp-1 text-[10px] leading-5 text-muted">{step.meta}</p>
              </div>
              {active && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-accent" aria-hidden="true" />}
            </button>
          )
        })}
      </div>
    </section>
  )
}

function StepDetailHeader({ step }: { step: ReviewStep }) {
  const Icon = step.icon
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3.5">
      <div className="flex items-start gap-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
          <Icon className="h-4 w-4" />
        </span>
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] font-semibold text-accent">STEP {step.number}</span>
            <h2 className="text-sm font-semibold text-foreground">{step.title}</h2>
            <StepStatusBadge status={step.status} />
          </div>
          <p className="mt-0.5 text-[11px] text-muted">{step.description}</p>
        </div>
      </div>
    </div>
  )
}

function PendingStepPreview({
  step,
  businessDate,
  strategyCount,
}: {
  step: ReviewStepId
  businessDate: string
  strategyCount: number
}) {
  const cutoff = businessDate || '所选交易日'
  const previews: Record<ReviewStepId, { title: string; lines: string[] }> = {
    market: {
      title: '等待生成市场环境',
      lines: [
        `读取不晚于 ${cutoff} 的指数、行情、技术指标和可证明时间归属的市场数据。`,
        `新闻统一截止到北京时间 ${cutoff} 23:59:59；发布时间未知或更晚的条目不会注入上下文。`,
      ],
    },
    candidates: {
      title: '等待运行策略候选',
      lines: [
        `使用选股页策略池中当前选定的 ${strategyCount} 个股票日线策略，在 ${cutoff} 的历史行情上筛选。`,
        '候选冻结后进入多 Agent 研究 Graph，并与持仓使用相同的事实、辩论和风险审查流程。',
      ],
    },
    positions: {
      title: '等待回放历史持仓',
      lines: [
        `只回放交易日不晚于 ${cutoff} 的买卖流水，按 FIFO 汇总当日持仓、成本和已实现盈亏。`,
        '每个冻结持仓进入可恢复研究 Graph；Bull 与 Bear 进行两轮交替辩论，失败节点可手动恢复。',
      ],
    },
  }
  const preview = previews[step]
  return (
    <div className="p-4">
      <div className="rounded-lg border border-dashed border-border bg-base p-4">
        <h3 className="text-xs font-semibold text-foreground">{preview.title}</h3>
        <div className="mt-2 space-y-1.5 text-[11px] leading-5 text-muted">
          {preview.lines.map(line => <p key={line}>{line}</p>)}
        </div>
        <p className="mt-3 text-[10px] text-secondary">点击页面右上角“新建复盘”后，本步骤会显示真实执行状态和完整结果。</p>
      </div>
    </div>
  )
}

const HISTORY_STATUS: Record<DailyReviewRoutine['status'], { label: string; dot: string }> = {
  running: { label: '进行中', dot: 'border-accent bg-accent ring-4 ring-accent/10' },
  completed: { label: '已完成', dot: 'border-success bg-success' },
  degraded: { label: '部分完成', dot: 'border-warning bg-warning' },
  failed: { label: '失败', dot: 'border-danger bg-danger' },
  interrupted: { label: '已中断', dot: 'border-warning bg-base' },
}

function formatRunTime(value: string) {
  return new Date(value).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function ReviewHistoryDrawer({
  open,
  items,
  selectedId,
  runningCount,
  onClose,
  onSelect,
}: {
  open: boolean
  items: DailyReviewRoutineSummary[]
  selectedId: string
  runningCount: number
  onClose: () => void
  onSelect: (item: DailyReviewRoutineSummary) => void
}) {
  const groups = Array.from(items.reduce(
    (result, item) => {
      const group = result.get(item.business_date)
      if (group) group.items.push(item)
      else result.set(item.business_date, { date: item.business_date, items: [item] })
      return result
    },
    new Map<string, { date: string; items: DailyReviewRoutineSummary[] }>(),
  ).values())
  if (!open) return null
  return (
    <>
      <button
        type="button"
        aria-label="收起复盘历史"
        onClick={onClose}
        className="fixed inset-0 z-40 cursor-default bg-black/35 backdrop-blur-[1px]"
      />
      <aside
        id="daily-review-history-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="daily-review-history-title"
        className="fixed inset-y-0 right-0 z-50 flex w-[min(380px,calc(100vw-1rem))] flex-col border-l border-border bg-surface shadow-2xl"
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-4">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-accent">
              <History className="h-3.5 w-3.5" />Review Ledger
            </div>
            <h2 id="daily-review-history-title" className="mt-1 text-sm font-semibold text-foreground">复盘历史</h2>
          </div>
          <div className="flex items-center gap-2">
            <span className={cn(
              'rounded-full border px-2 py-1 font-mono text-[9px]',
              runningCount > 0
                ? 'border-accent/30 bg-accent/10 text-accent'
                : 'border-border bg-elevated text-muted',
            )}>
              {runningCount > 0 ? `${runningCount} 个进行中` : `${items.length} 次`}
            </span>
            <button
              type="button"
              onClick={onClose}
              className="grid h-8 w-8 place-items-center rounded-btn border border-border bg-base text-muted transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label="关闭复盘历史"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {groups.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-3 py-8 text-center">
            <div className="text-xs font-medium text-secondary">还没有复盘历史</div>
            <p className="mt-1 text-[10px] leading-5 text-muted">选择交易日并新建复盘后，每次运行都会独立保留在这里。</p>
          </div>
        ) : groups.map(group => (
          <section key={group.date} className="mb-4 last:mb-0">
            <div className="mb-1.5 flex items-center justify-between px-1">
              <h3 className="font-mono text-[10px] font-semibold text-secondary">{group.date}</h3>
              <span className="text-[9px] text-muted">{group.items.length} 次</span>
            </div>
            <div className="relative space-y-1 before:absolute before:bottom-3 before:left-[13px] before:top-3 before:w-px before:bg-border">
              {group.items.map(item => {
                const active = item.id === selectedId
                const status = HISTORY_STATUS[item.status]
                return (
                  <button
                    key={item.id}
                    type="button"
                    aria-current={active ? 'true' : undefined}
                    onClick={() => onSelect(item)}
                    className={cn(
                      'group relative flex w-full items-start gap-3 rounded-lg py-2.5 pl-1.5 pr-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                      active ? 'bg-accent/10' : 'hover:bg-elevated/65',
                    )}
                  >
                    <span className={cn('relative z-10 mt-1 h-3.5 w-3.5 shrink-0 rounded-full border-2', status.dot)} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center justify-between gap-2">
                        <span className={cn('text-[11px] font-semibold', active ? 'text-foreground' : 'text-secondary')}>
                          第 {item.run_number} 次复盘
                        </span>
                        <span className="font-mono text-[9px] text-muted">{formatRunTime(item.created_at)}</span>
                      </span>
                      <span className="mt-1 flex items-center justify-between gap-2 text-[9px] text-muted">
                        <span>{status.label}</span>
                        <span>{item.completed_target_count}/{item.target_count} 分析完成</span>
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          </section>
        ))}
        </div>
      </aside>
    </>
  )
}

type RetryTarget = {
  target_type: 'position' | 'candidate'
  source_ref: string
  node_id: string
}

export function DailyReview() {
  const queryClient = useQueryClient()
  const routineIdRef = useRef<string | null>(null)
  const historyInitializedRef = useRef(false)
  const [businessDate, setBusinessDate] = useState('')
  const [selectedRoutineId, setSelectedRoutineId] = useState('')
  const [activeStep, setActiveStep] = useState<ReviewStepId>('market')
  const [selectedCandidateRef, setSelectedCandidateRef] = useState('')
  const [selectedPositionRef, setSelectedPositionRef] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [previewStock, setPreviewStock] = useState<{ symbol: string; name: string } | null>(null)
  const {
    pool: screenerPool,
    isReady: strategyPoolReady,
    isSaving: strategyPoolSaving,
    isError: strategyPoolError,
    errorKind: strategyPoolErrorKind,
    retry: retryStrategyPool,
  } = useStrategyPool()
  const strategyPoolErrorLabel = strategyPoolErrorKind === 'save' ? '策略池保存失败' : '策略池加载失败'
  const strategyPoolPendingLabel = strategyPoolSaving ? '正在保存策略池' : '正在恢复策略池'
  const tradingDatesQuery = useTradingDates()
  const tradingDates = tradingDatesQuery.data?.dates ?? []
  const historyQuery = useQuery({
    queryKey: QK.dailyReviewList,
    queryFn: api.dailyReviewList,
    refetchInterval: query => (query.state.data?.running_count ?? 0) > 0 ? 2_000 : false,
  })
  const historyItems = historyQuery.data?.items ?? []
  const runningCount = historyQuery.data?.running_count ?? 0
  const strategiesQuery = useQuery({
    queryKey: QK.screenerStrategies('all'),
    queryFn: () => api.screenerStrategies(),
  })
  const reviewStrategies = useMemo(
    () => (strategiesQuery.data?.presets ?? []).filter(strategy => (
      strategy.asset_types.includes('stock') && strategy.timeframes.includes('1d')
    )),
    [strategiesQuery.data?.presets],
  )
  const reviewStrategyIds = useMemo(() => {
    const available = new Set(reviewStrategies.map(strategy => strategy.id))
    return screenerPool.filter(strategyId => available.has(strategyId))
  }, [reviewStrategies, screenerPool])
  const strategyNameById = useMemo(
    () => new Map(reviewStrategies.map(strategy => [strategy.id, strategy.name])),
    [reviewStrategies],
  )

  useEffect(() => {
    const latest = tradingDatesQuery.data?.latest_date
    if (!latest) return
    setBusinessDate(current => current && tradingDates.includes(current) ? current : latest)
  }, [tradingDates, tradingDatesQuery.data?.latest_date])

  useEffect(() => {
    if (!historyQuery.isSuccess) return
    if (historyInitializedRef.current) return
    const latestRoutine = historyItems[0]
    if (latestRoutine) {
      setSelectedRoutineId(latestRoutine.id)
      setBusinessDate(latestRoutine.business_date)
    }
    historyInitializedRef.current = true
  }, [historyItems, historyQuery.isSuccess])

  const routineQuery = useQuery({
    queryKey: QK.dailyReview(selectedRoutineId),
    queryFn: () => api.dailyReviewGet(selectedRoutineId),
    enabled: selectedRoutineId.length > 0,
    refetchInterval: query => query.state.data?.routine?.status === 'running' ? 2_000 : false,
  })
  const routine = routineQuery.data?.routine ?? null

  useEffect(() => {
    if (routineIdRef.current === (routine?.id ?? null)) return
    routineIdRef.current = routine?.id ?? null
    if (!routine) {
      setActiveStep('market')
      return
    }
    setActiveStep(initialStep(routine))
  }, [routine])

  useEffect(() => {
    if (!routine) return
    setSelectedCandidateRef(current => (
      routine.candidates.some(item => item.source_ref === current)
        ? current
        : routine.candidates.find(item => ['failed', 'interrupted'].includes(item.status))?.source_ref
          ?? routine.candidates[0]?.source_ref
          ?? ''
    ))
    setSelectedPositionRef(current => (
      routine.positions.some(item => item.source_ref === current)
        ? current
        : routine.positions.find(item => ['failed', 'interrupted'].includes(item.status))?.source_ref
          ?? routine.positions[0]?.source_ref
          ?? ''
    ))
  }, [routine])

  useEffect(() => {
    if (!historyOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setHistoryOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [historyOpen])

  const runMutation = useMutation({
    mutationFn: () => api.dailyReviewRun(businessDate, reviewStrategyIds),
    onSuccess: created => {
      setSelectedRoutineId(created.id)
      queryClient.setQueryData(QK.dailyReview(created.id), { routine: created })
      queryClient.invalidateQueries({ queryKey: QK.dailyReviewList })
      toast(`${created.business_date} 第 ${created.run_number} 次复盘已启动`, 'success')
    },
  })
  const retryMutation = useMutation({
    mutationFn: () => api.dailyReviewRetry(routine!.id),
    onSuccess: restarted => {
      queryClient.setQueryData(QK.dailyReview(restarted.id), { routine: restarted })
      queryClient.invalidateQueries({ queryKey: QK.dailyReviewList })
      toast('失败或中断项已重新提交', 'success')
    },
  })
  const graphRetryMutation = useMutation({
    mutationFn: (target: RetryTarget) => api.dailyReviewGraphRetry(routine!.id, target),
    onSuccess: restarted => {
      queryClient.setQueryData(QK.dailyReview(restarted.id), { routine: restarted })
      queryClient.invalidateQueries({ queryKey: QK.dailyReviewList })
      toast('已从指定节点恢复', 'success')
    },
  })
  const interruptMutation = useMutation({
    mutationFn: api.dailyReviewInterruptAll,
    onSuccess: result => {
      queryClient.invalidateQueries({ queryKey: ['daily-review'] })
      toast(
        result.interrupted_count > 0
          ? `已中断 ${result.interrupted_count} 个进行中的复盘`
          : '当前没有进行中的复盘',
        'success',
      )
    },
  })

  const selectedCandidate = routine?.candidates.find(item => item.source_ref === selectedCandidateRef)
    ?? routine?.candidates[0]
  const selectedPosition = routine?.positions.find(item => item.source_ref === selectedPositionRef)
    ?? routine?.positions[0]
  const currentPositionStepStatus = routine ? positionStepStatus(routine) : 'pending'
  const completedTargets = useMemo(
    () => routine
      ? [...routine.positions, ...routine.candidates].filter(item => item.status === 'completed').length
      : 0,
    [routine],
  )
  const totalTargets = (routine?.positions.length ?? 0) + (routine?.candidates.length ?? 0)
  const needsGraphUpgrade = Boolean(
    routine && (
      routine.strategy_screening.status === 'skipped'
      || (routine.market_review.status === 'completed'
        && routine.market_review.report?.point_in_time_version !== 1)
      || routine.positions.some(
        item => !item.graph || item.graph.schema_version < DAILY_REVIEW_GRAPH_SCHEMA_VERSION,
      )
      || routine.candidates.some(
        item => item.graph.schema_version < DAILY_REVIEW_GRAPH_SCHEMA_VERSION,
      )
    ),
  )
  const canRetry = routine?.status === 'degraded' || routine?.status === 'failed' || routine?.status === 'interrupted'
  const mutationPending = runMutation.isPending || retryMutation.isPending || interruptMutation.isPending
  const reviewSteps = useMemo<ReviewStep[]>(() => {
    if (!routine) return [
      {
        id: 'market',
        number: '01',
        title: '市场环境',
        description: '生成截止日市场背景与新闻证据',
        meta: '等待市场行情与截止日新闻',
        status: 'pending',
        icon: FileText,
      },
      {
        id: 'candidates',
        number: '02',
        title: '策略候选',
        description: '运行选股页策略池并冻结历史候选',
        meta: strategyPoolReady
          ? `将使用策略池中的 ${reviewStrategyIds.length} 个股票日线策略`
          : strategyPoolError ? strategyPoolErrorLabel : strategyPoolPendingLabel,
        status: 'pending',
        icon: Sparkles,
      },
      {
        id: 'positions',
        number: '03',
        title: '持仓分析',
        description: '回放交易流水并分析历史持仓',
        meta: '等待 FIFO 持仓回放与研究 Graph',
        status: 'pending',
        icon: Layers3,
      },
    ]
    const candidateStatus = candidateStepStatus(routine)
    const positionStatus = positionStepStatus(routine)
    const completedCandidates = routine.candidates.filter(item => item.status === 'completed').length
    const completedPositions = routine.positions.filter(item => item.status === 'completed').length
    return [
      {
        id: 'market',
        number: '01',
        title: '市场环境',
        description: '复用现有大盘复盘，作为全部目标共享的市场背景',
        meta: routine.market_review.report
          ? `大盘报告已生成 · ${routine.news_context.item_count} 条截止日新闻`
          : routine.market_review.error ? '报告生成失败' : '等待大盘报告',
        status: routine.market_review.status,
        icon: FileText,
      },
      {
        id: 'candidates',
        number: '02',
        title: '策略候选',
        description: '按策略池与策略原始结果顺序冻结候选，再进入研究 Graph',
        meta: candidateStatus === 'skipped'
          ? '旧版档案需要补齐'
          : `${routine.candidates.length} 个候选 · ${completedCandidates}/${routine.candidates.length} 已分析`,
        status: candidateStatus,
        icon: Sparkles,
      },
      {
        id: 'positions',
        number: '03',
        title: '持仓分析',
        description: '冻结账户与成本事实，与策略候选共用可观察、可恢复的研究 Graph',
        meta: positionStatus === 'blocked'
          ? '等待策略候选步骤完成'
          : routine.positions.length === 0
          ? '冻结范围内没有持仓'
          : `${routine.positions.length} 项持仓 · ${completedPositions}/${routine.positions.length} 已分析`,
        status: positionStatus,
        icon: Layers3,
      },
    ]
  }, [reviewStrategyIds.length, routine, strategyPoolError, strategyPoolErrorLabel, strategyPoolPendingLabel, strategyPoolReady])
  const selectedStep = reviewSteps.find(step => step.id === activeStep) ?? reviewSteps[0]

  const retryNode = (
    targetType: 'position' | 'candidate',
    sourceRef: string,
    node: DailyReviewGraphNode,
  ) => graphRetryMutation.mutate({ target_type: targetType, source_ref: sourceRef, node_id: node.id })

  return (
    <div className="min-h-full bg-base">
      <PageHeader
        title="每日复盘"
        titleExtra={<ClipboardCheck className="h-4 w-4 text-accent" />}
        subtitle="策略候选与持仓进入 TradingAgents 风格研究 Graph,实时呈现拓扑、数据流和节点输入输出"
        className="flex-wrap items-start [&>div:first-child]:min-w-0 [&>div:first-child]:flex-1 [&>div:first-child]:flex-wrap lg:flex-nowrap lg:items-center"
        right={(
          <div className="flex w-full max-w-full flex-wrap items-center justify-start gap-2 lg:w-auto lg:justify-end">
            <DatePicker
              value={businessDate}
              onChange={value => {
                setBusinessDate(value)
                setSelectedRoutineId('')
              }}
              min={tradingDatesQuery.data?.earliest_date ?? undefined}
              max={tradingDatesQuery.data?.latest_date ?? undefined}
              availableDates={tradingDates}
              disabled={tradingDates.length === 0}
              placeholder={tradingDatesQuery.isError ? '交易日加载失败' : tradingDatesQuery.isLoading ? '加载交易日…' : '暂无交易日'}
            />
            <button
              type="button"
              aria-controls="daily-review-history-drawer"
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-surface px-3 py-2 text-xs font-medium text-secondary transition-colors hover:border-accent/30 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              title="从右侧打开复盘历史"
            >
              <History className="h-3.5 w-3.5" />
              复盘历史
              <span className="font-mono text-[9px] text-muted">{historyItems.length}</span>
            </button>
            <button
              onClick={() => {
                historyQuery.refetch()
                if (selectedRoutineId) routineQuery.refetch()
              }}
              className="rounded-btn border border-border bg-surface p-2 text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              title="刷新复盘历史和当前状态"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', (routineQuery.isFetching || historyQuery.isFetching) && 'animate-spin')} />
            </button>
            {runningCount > 0 && (
              <button
                type="button"
                onClick={() => interruptMutation.mutate()}
                disabled={interruptMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-btn border border-danger/35 bg-danger/5 px-3 py-2 text-xs font-medium text-danger transition-colors hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-50"
                title="中断市场复盘、策略筛选以及所有正在等待的 Agent 分析任务"
              >
                {interruptMutation.isPending
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Square className="h-3.5 w-3.5 fill-current" />}
                中断全部（{runningCount}）
              </button>
            )}
            <button
              onClick={() => {
                if (strategyPoolError) {
                  retryStrategyPool()
                  return
                }
                runMutation.mutate()
              }}
              disabled={!strategyPoolError && (!businessDate || runMutation.isPending || interruptMutation.isPending || !strategiesQuery.isSuccess || !strategyPoolReady)}
              className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3.5 py-2 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
              title={strategyPoolReady
                ? `新建一次独立复盘，并冻结策略池中的 ${reviewStrategyIds.length} 个策略`
                : strategyPoolError ? `${strategyPoolErrorLabel}，点击重试` : strategyPoolPendingLabel}
            >
              {!strategyPoolReady
                ? strategyPoolError
                  ? <><AlertTriangle className="h-3.5 w-3.5" />{strategyPoolErrorLabel}</>
                  : <><Loader2 className="h-3.5 w-3.5 animate-spin" />{strategyPoolSaving ? '保存策略池' : '加载策略池'}</>
                : runMutation.isPending
                ? <><Loader2 className="h-3.5 w-3.5 animate-spin" />正在新建</>
                : <><Play className="h-3.5 w-3.5" />新建复盘</>}
            </button>
          </div>
        )}
      />

      <main className="mx-auto max-w-[1480px] p-4 md:p-5">
        <div className="min-w-0 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-border bg-surface px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent/10 text-accent">
              <Clock3 className="h-4 w-4" />
            </div>
            <div>
              <div className="text-sm font-medium text-foreground">
                {routine
                  ? `${routine.business_date} · 第 ${routine.run_number} 次复盘`
                  : `${businessDate || '最近交易日'} · 新复盘预览`}
              </div>
              <div className="mt-0.5 text-[11px] text-muted">
                {routine
                  ? `多 Agent 分析 ${completedTargets}/${totalTargets} · 策略候选 ${routine.candidates.length} · 更新于 ${new Date(routine.updated_at).toLocaleString('zh-CN')}`
                  : `尚未生成；将依次运行市场环境、${reviewStrategyIds.length} 个策略的候选分析和持仓分析`}
              </div>
            </div>
          </div>
          {routine && (
            <div className="flex items-center gap-2">
              <RoutineStatus status={routine.status} />
              {canRetry && (
                <button
                  type="button"
                  onClick={() => retryMutation.mutate()}
                  disabled={mutationPending}
                  className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-base px-2.5 py-1.5 text-[10px] font-medium text-secondary hover:border-accent/30 hover:text-foreground disabled:opacity-50"
                >
                  {retryMutation.isPending
                    ? <Loader2 className="h-3 w-3 animate-spin" />
                    : <RefreshCw className="h-3 w-3" />}
                  {routine.status === 'interrupted' ? '从中断处继续' : '重试失败项'}
                </button>
              )}
            </div>
          )}
        </div>

        {routine && needsGraphUpgrade && (
          <div className="flex items-start gap-2 rounded-card border border-warning/25 bg-warning/5 px-4 py-3 text-[11px] leading-5 text-secondary">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            这是旧版复盘档案，尚未完整通过当前日期截止规则。请保留它作为历史证据，并使用右上角“新建复盘”按当前流程生成一个独立实例。
          </div>
        )}

        {tradingDatesQuery.isLoading || (selectedRoutineId.length > 0 && routineQuery.isLoading) ? (
          <div className="grid min-h-64 place-items-center rounded-card border border-border bg-surface">
            <Loader2 className="h-5 w-5 animate-spin text-muted" />
          </div>
        ) : !routine ? (
          <>
            <ReviewStepper steps={reviewSteps} activeStep={activeStep} onSelect={setActiveStep} />
            {selectedStep && (
              <section
                id={`daily-review-step-${selectedStep.id}`}
                role="tabpanel"
                tabIndex={0}
                aria-labelledby={`daily-review-step-tab-${selectedStep.id}`}
                className="overflow-hidden rounded-card border border-border bg-surface"
              >
                <StepDetailHeader step={selectedStep} />
                <PendingStepPreview step={activeStep} businessDate={businessDate} strategyCount={reviewStrategyIds.length} />
              </section>
            )}
            <div className="flex items-start gap-2 rounded-card border border-warning/20 bg-warning/5 px-4 py-3 text-[11px] leading-5 text-secondary">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
              每日复盘、策略候选和多 Agent 分析只用于研究记录，不构成投资建议、买卖建议或仓位建议。策略命中不是收益承诺，持仓估值只使用复盘日当天的本地收盘价；当天缺价的持仓不计入市值和浮动盈亏。
            </div>
          </>
        ) : (
          <>
            <ReviewStepper steps={reviewSteps} activeStep={activeStep} onSelect={setActiveStep} />

            {selectedStep && (
              <section
                id={`daily-review-step-${selectedStep.id}`}
                role="tabpanel"
                tabIndex={0}
                aria-labelledby={`daily-review-step-tab-${selectedStep.id}`}
                className="overflow-hidden rounded-card border border-border bg-surface"
              >
                <StepDetailHeader step={selectedStep} />

                {activeStep === 'market' && (
                  <div className="p-4">
                    <div className="mb-3 flex items-center gap-2 text-xs text-secondary">
                      <FileText className="h-3.5 w-3.5" />大盘复盘报告
                    </div>
                    {routine.market_review.report ? (
                      <div className="rounded-lg border border-border bg-base px-4 pb-4">
                        <MarkdownRenderer content={routine.market_review.report.content} />
                      </div>
                    ) : routine.market_review.error ? (
                      <ErrorPanel message={routine.market_review.error} />
                    ) : <PendingPanel label="等待市场报告…" />}
                    <div className="mt-4 rounded-lg border border-border bg-base p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="text-xs font-medium text-foreground">新闻证据</div>
                        <div className="font-mono text-[10px] text-muted">
                          截止 {routine.news_context.cutoff_at ?? `${routine.business_date} 23:59:59`} · {routine.news_context.item_count} 条
                        </div>
                      </div>
                      <p className="mt-1 text-[10px] leading-5 text-muted">
                        只注入发布时间不晚于复盘日的归档新闻；发布时间未知的条目也会排除。来源抓取状态：{routine.news_context.source_status}。
                      </p>
                      {routine.news_context.items.length > 0 ? (
                        <div className="mt-2 grid gap-2 md:grid-cols-2">
                          {routine.news_context.items.slice(0, 8).map(item => (
                            <a key={item.id} href={item.url} target="_blank" rel="noreferrer" className="rounded-btn border border-border px-2.5 py-2 text-[11px] text-secondary hover:border-accent/30 hover:text-foreground">
                              <div className="line-clamp-1 font-medium">{item.title}</div>
                              <div className="mt-1 font-mono text-[9px] text-muted">{item.source} · {item.published_date}</div>
                            </a>
                          ))}
                        </div>
                      ) : (
                        <div className="mt-2 rounded-btn border border-dashed border-border px-3 py-2 text-[10px] text-muted">截止该交易日没有可证明时间归属的新闻，News Analyst 会明确记录数据缺口。</div>
                      )}
                      {routine.news_context.errors.length > 0 && (
                        <div className="mt-2 text-[10px] text-warning">部分新闻源不可用：{routine.news_context.errors.join('；')}</div>
                      )}
                    </div>
                  </div>
                )}

                {activeStep === 'candidates' && (
                  <div className="p-4">
                    <div>
                      {routine.strategy_screening.status === 'skipped' ? (
                        <div className="flex items-start gap-3 rounded-lg border border-warning/25 bg-warning/5 p-4">
                          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                          <div>
                            <h3 className="text-xs font-semibold text-foreground">旧版档案尚未生成策略候选</h3>
                            <p className="mt-1 text-[11px] leading-5 text-muted">
                              保留本次历史档案，并使用页面右上角“新建复盘”按当前策略池生成新的候选与研究 Graph。
                            </p>
                          </div>
                        </div>
                      ) : routine.strategy_screening.status === 'blocked' ? (
                        <BlockedPanel message={routine.strategy_screening.error ?? '等待市场环境步骤成功后再运行策略候选。'} />
                      ) : routine.strategy_screening.error ? (
                        <ErrorPanel message={routine.strategy_screening.error} />
                      ) : routine.strategy_screening.status !== 'completed' ? (
                        <PendingPanel label="正在运行策略筛选…" />
                      ) : routine.candidates.length === 0 ? (
                        <div className="space-y-4">
                          <ScreeningProvenance
                            screening={routine.strategy_screening}
                            strategyNameById={strategyNameById}
                          />
                          <div className="rounded-lg border border-dashed border-border p-6 text-center text-xs leading-5 text-muted">
                            这组冻结策略在当日没有候选。空结果会保留，不会用全部策略、自选股或持仓补位。
                          </div>
                        </div>
                      ) : (
                        <div className="space-y-4">
                          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                            {routine.candidates.map(candidate => (
                              <CandidateButton
                                key={candidate.source_ref}
                                candidate={candidate}
                                selected={candidate.source_ref === selectedCandidate?.source_ref}
                                onSelect={() => setSelectedCandidateRef(candidate.source_ref)}
                                onOpenChart={() => setPreviewStock({
                                  symbol: candidate.symbol,
                                  name: candidate.name || candidate.symbol,
                                })}
                              />
                            ))}
                          </div>
                          {selectedCandidate && (
                            <>
                              <CandidateConclusion
                                candidate={selectedCandidate}
                                onOpenChart={() => setPreviewStock({
                                  symbol: selectedCandidate.symbol,
                                  name: selectedCandidate.name || selectedCandidate.symbol,
                                })}
                              />
                              <div className="flex flex-wrap items-end justify-between gap-2 border-t border-border pt-4">
                                <div>
                                  <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">Analysis Detail</div>
                                  <h3 className="mt-1 text-xs font-semibold text-foreground">候选来源与完整研究过程</h3>
                                </div>
                                <span className="text-[10px] text-muted">以下内容用于追溯结论，不影响候选顺序</span>
                              </div>
                              <ScreeningProvenance
                                screening={routine.strategy_screening}
                                strategyNameById={strategyNameById}
                              />
                              <CandidateProvenanceTable candidate={selectedCandidate} />
                              <DailyReviewAnalysisGraphView
                                key={selectedCandidate.graph.id}
                                graph={selectedCandidate.graph}
                                title={`${selectedCandidate.name || selectedCandidate.symbol} · 策略候选分析`}
                                subtitle={`${selectedCandidate.symbol} · ${selectedCandidate.reason}`}
                                retryingNodeId={graphRetryMutation.variables?.source_ref === selectedCandidate.source_ref ? graphRetryMutation.variables.node_id : null}
                                onRetryNode={node => retryNode('candidate', selectedCandidate.source_ref, node)}
                              />
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {activeStep === 'positions' && (
                  currentPositionStepStatus === 'blocked' ? (
                    <div className="p-4">
                      <BlockedPanel message={routine.positions.find(item => item.status === 'blocked')?.error ?? '等待策略候选步骤成功后再运行持仓分析。'} />
                    </div>
                  ) : (
                    <>
                      <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
                        <Metric icon={Layers3} label="持仓数" value={String(routine.scope_summary.position_count)} detail={`${routine.scope_summary.trade_count} 笔交易回放`} />
                        <Metric icon={CircleDollarSign} label="总成本" value={formatMoney(routine.scope_summary.total_cost)} detail={`估值成本 ${formatMoney(routine.scope_summary.valuation_cost)}`} />
                        <Metric icon={ShieldCheck} label="市值" value={formatMoney(routine.scope_summary.market_value)} detail={`缺价 ${routine.scope_summary.missing_price_count}`} />
                        <Metric icon={CheckCircle2} label="浮动盈亏" value={formatMoney(routine.scope_summary.unrealized_pnl)} detail={`已实现 ${formatMoney(routine.scope_summary.realized_pnl)}`} />
                      </div>

                      {routine.positions.length === 0 ? (
                        <div className="border-t border-border p-4">
                          <div className="rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted">冻结范围内没有持仓，本步骤无需生成。</div>
                        </div>
                      ) : (
                        <>
                          <div className="overflow-x-auto border-y border-border">
                            <table className="w-full min-w-[900px] text-left text-xs">
                              <thead className="bg-elevated/70 text-[11px] text-muted">
                                <tr><th className="px-4 py-2.5">账户 / 标的</th><th className="px-3 py-2.5">数量</th><th className="px-3 py-2.5">成本价</th><th className="px-3 py-2.5">当日收盘价</th><th className="px-3 py-2.5">市值</th><th className="px-3 py-2.5">盈亏</th><th className="px-3 py-2.5">分析</th></tr>
                              </thead>
                              <tbody className="divide-y divide-border">
                                {routine.positions.map(item => (
                                  <PositionRow
                                    key={item.source_ref}
                                    item={item}
                                    selected={item.source_ref === selectedPosition?.source_ref}
                                    onSelect={() => setSelectedPositionRef(item.source_ref)}
                                  />
                                ))}
                              </tbody>
                            </table>
                          </div>
                          {selectedPosition && (
                            <div className="p-4">
                              <DailyReviewAnalysisGraphView
                                key={selectedPosition.graph?.id ?? selectedPosition.source_ref}
                                graph={selectedPosition.graph}
                                title={`${selectedPosition.name || selectedPosition.symbol} · 持仓分析`}
                                subtitle={`${selectedPosition.account_name} · ${selectedPosition.symbol} · 冻结成本 ${formatMoney(selectedPosition.total_cost)}`}
                                retryingNodeId={graphRetryMutation.variables?.source_ref === selectedPosition.source_ref ? graphRetryMutation.variables.node_id : null}
                                onRetryNode={node => retryNode('position', selectedPosition.source_ref, node)}
                              />
                            </div>
                          )}
                        </>
                      )}
                    </>
                  )
                )}
              </section>
            )}

            <div className="flex items-start gap-2 rounded-card border border-warning/20 bg-warning/5 px-4 py-3 text-[11px] leading-5 text-secondary">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
              每日复盘、策略候选和多 Agent 分析只用于研究记录，不构成投资建议、买卖建议或仓位建议。策略命中不是收益承诺，持仓估值只使用复盘日当天的本地收盘价；当天缺价的持仓不计入市值和浮动盈亏。
            </div>
          </>
        )}
        </div>
      </main>
      <ReviewHistoryDrawer
        open={historyOpen}
        items={historyItems}
        selectedId={selectedRoutineId}
        runningCount={runningCount}
        onClose={() => setHistoryOpen(false)}
        onSelect={item => {
          setSelectedRoutineId(item.id)
          setBusinessDate(item.business_date)
          setHistoryOpen(false)
        }}
      />
      <StockPreviewDialog
        symbol={previewStock?.symbol ?? null}
        name={previewStock?.name}
        onClose={() => setPreviewStock(null)}
      />
    </div>
  )
}

function RoutineStatus({ status }: { status: 'running' | 'completed' | 'degraded' | 'failed' | 'interrupted' }) {
  return (
    <span className={cn(
      'rounded-full border px-2.5 py-1 text-[11px] font-medium',
      status === 'completed' && 'border-success/30 bg-success/10 text-success',
      status === 'running' && 'border-accent/30 bg-accent/10 text-accent',
      status === 'degraded' && 'border-warning/30 bg-warning/10 text-warning',
      (status === 'failed' || status === 'interrupted') && 'border-danger/30 bg-danger/10 text-danger',
    )}>
      {status === 'completed' ? '复盘完成' : status === 'running' ? '生成中' : status === 'degraded' ? '部分完成' : '复盘未完成'}
    </span>
  )
}

function ScreeningProvenance({
  screening,
  strategyNameById,
}: {
  screening: DailyReviewRoutine['strategy_screening']
  strategyNameById: Map<string, string>
}) {
  const strategies = screening.strategies.length > 0
    ? screening.strategies.map(strategy => ({
      ...strategy,
      name: strategy.name === strategy.id
        ? strategyNameById.get(strategy.id) ?? strategy.name
        : strategy.name,
    }))
    : screening.strategy_ids.map(id => ({ id, name: strategyNameById.get(id) ?? id }))
  const sourceLabel = screening.selection_source === 'screener_pool'
    ? '选股页策略池 · 启动时冻结'
    : screening.selection_source === 'all_available'
      ? '全部可用日线策略 · 历史口径'
      : '旧版档案 · 来源未记录'
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-base">
      <div className="grid gap-px bg-border lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <div className="bg-base p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">Strategy Snapshot</div>
              <h3 className="mt-1 text-xs font-semibold text-foreground">候选来源</h3>
            </div>
            <span className="rounded-full border border-border bg-elevated px-2 py-1 font-mono text-[9px] text-secondary">
              {sourceLabel}
            </span>
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {strategies.length > 0 ? strategies.map(strategy => (
              <span key={strategy.id} title={strategy.id} className="rounded-full border border-border bg-surface px-2 py-1 text-[9px] text-secondary">
                {strategy.name}
              </span>
            )) : (
              <span className="text-[11px] text-muted">策略池为空，本步骤会保留 0 个候选，不会自动改用全部策略。</span>
            )}
          </div>
        </div>
        <div className="bg-base p-3.5">
          <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">Selection Order</div>
          <h3 className="mt-1 text-xs font-semibold text-foreground">候选顺序</h3>
          <ol className="mt-2 space-y-1 font-mono text-[10px] leading-5 text-muted">
            <li><span className="mr-2 text-accent">01</span>遵循策略池保存顺序</li>
            <li><span className="mr-2 text-accent">02</span>遵循各策略原始选股顺序</li>
            <li><span className="mr-2 text-accent">03</span>股票首次出现即固定位置，去重后最多 8 个</li>
          </ol>
        </div>
      </div>
    </div>
  )
}

function CandidateConclusion({
  candidate,
  onOpenChart,
}: {
  candidate: DailyReviewCandidate
  onOpenChart: () => void
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-accent/30 bg-accent/[0.035]">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-accent/20 px-4 py-3.5">
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-accent">Final Research Verdict</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">多 Agent 最终结论</h3>
            <span className="font-mono text-[10px] text-muted">候选 {String(candidate.rank).padStart(2, '0')}</span>
          </div>
          <p className="mt-1 text-[10px] leading-5 text-muted">{candidate.reason}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenChart}
            className="inline-flex items-center gap-1.5 rounded-btn border border-accent/25 bg-base px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:border-accent/50 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            title={`查看 ${candidate.symbol} K 线图`}
          >
            <LineChart className="h-3.5 w-3.5" />
            {candidate.name || candidate.symbol}
          </button>
          <StatusBadge status={candidate.status} />
        </div>
      </div>
      <div className="bg-base/70 px-4 pb-4">
        {candidate.report ? (
          <MarkdownRenderer content={candidate.report.content} />
        ) : candidate.error ? (
          <div className="pt-4"><ErrorPanel message={candidate.error} /></div>
        ) : (
          <div className="pt-4"><PendingPanel label="等待多 Agent 最终结论…" /></div>
        )}
      </div>
    </section>
  )
}

function CandidateProvenanceTable({ candidate }: { candidate: DailyReviewCandidate }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-base">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3.5 py-3">
        <div>
          <h3 className="text-xs font-semibold text-foreground">{candidate.name || candidate.symbol} 的候选依据</h3>
          <p className="mt-0.5 text-[10px] text-muted">保留每个命中策略的原始排名与原始分；多策略命中只作为证据，不参与重排。</p>
        </div>
        <div className="flex items-center gap-3 font-mono text-[10px]">
          <span className="text-secondary">命中 <strong className="text-foreground">{candidate.matched_strategies.length}</strong> 个策略</span>
          <span className="text-secondary">候选顺序 <strong className="text-accent">#{candidate.rank}</strong></span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] text-left text-[10px]">
          <thead className="bg-elevated/70 text-muted">
            <tr>
              <th className="px-3.5 py-2 font-medium">命中策略</th>
              <th className="px-3 py-2 font-medium">策略内排名</th>
              <th className="px-3 py-2 font-medium">策略原始分</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {candidate.matched_strategies.map(strategy => (
              <tr key={strategy.id}>
                <td className="px-3.5 py-2.5"><div className="font-medium text-foreground">{strategy.name}</div><div className="font-mono text-[9px] text-muted">{strategy.id}</div></td>
                <td className="px-3 py-2.5 font-mono text-secondary">#{strategy.rank}</td>
                <td className="px-3 py-2.5 font-mono text-secondary">{strategy.score == null ? '—' : strategy.score.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function CandidateButton({ candidate, selected, onSelect, onOpenChart }: {
  candidate: DailyReviewCandidate
  selected: boolean
  onSelect: () => void
  onOpenChart: () => void
}) {
  return (
    <div
      className={cn(
        'rounded-xl border p-3 text-left transition-colors',
        selected ? 'border-accent/45 bg-accent/10' : 'border-border bg-base hover:border-accent/25',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono text-lg font-semibold text-accent">{String(candidate.rank).padStart(2, '0')}</span>
        <StatusBadge status={candidate.status} />
      </div>
      <button
        type="button"
        onClick={onOpenChart}
        className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium text-foreground decoration-accent/60 underline-offset-4 transition-colors hover:text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        title={`查看 ${candidate.symbol} K 线图`}
      >
        {candidate.name || candidate.symbol}<LineChart className="h-3.5 w-3.5" />
      </button>
      <div className="mt-0.5 font-mono text-[10px] text-muted">{candidate.symbol}</div>
      <div className="mt-2 flex flex-wrap gap-1">
        {candidate.matched_strategies.slice(0, 3).map(strategy => (
          <span key={strategy.id} className="rounded-full bg-elevated px-2 py-0.5 text-[9px] text-secondary">{strategy.name}</span>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px]">
        <span className="font-medium text-foreground">命中 {candidate.matched_strategies.length} 个策略</span>
        <button
          type="button"
          onClick={onSelect}
          className={cn(
            'inline-flex items-center gap-1 rounded-btn border px-2 py-1 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            selected
              ? 'border-accent/35 bg-accent/10 text-accent'
              : 'border-border bg-surface text-secondary hover:text-foreground',
          )}
        >
          <ScanSearch className="h-3 w-3" />{selected ? '当前结论' : '查看结论'}
        </button>
      </div>
    </div>
  )
}

function PositionRow({ item, selected, onSelect }: {
  item: DailyReviewPosition
  selected: boolean
  onSelect: () => void
}) {
  return (
    <tr className={cn(selected && 'bg-accent/5')}>
      <td className="px-4 py-3"><div className="font-medium text-foreground">{item.name || item.symbol}</div><div className="mt-0.5 text-[10px] text-muted">{item.account_name} · {item.symbol}{item.purchase_date ? ` · 买入 ${item.purchase_date}` : ''}</div></td>
      <td className="px-3 py-3 font-mono text-secondary">{item.quantity}</td>
      <td className="px-3 py-3 font-mono text-secondary">{formatMoney(item.average_cost)}</td>
      <td className="px-3 py-3 font-mono text-secondary"><div>{formatMoney(item.current_price)}</div><div className={cn('mt-0.5 text-[10px]', item.price_available ? item.price_stale ? 'text-warning' : 'text-muted' : 'text-danger')}>{item.price_available ? `收盘价 · ${item.price_date ?? '日期未知'}${item.price_stale ? ' · 历史档案非当日价' : ''}` : '当日收盘价缺失'}</div></td>
      <td className="px-3 py-3 font-mono text-secondary">{formatMoney(item.market_value)}</td>
      <td className={cn('px-3 py-3 font-mono', (item.unrealized_pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>{formatMoney(item.unrealized_pnl)}<div className="text-[10px]">{formatRatio(item.unrealized_return_ratio)}</div></td>
      <td className="px-3 py-3">
        <button
          type="button"
          onClick={onSelect}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-btn border px-2.5 py-1.5 text-[10px] font-medium',
            selected ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-surface text-secondary hover:text-foreground',
          )}
        >
          <ScanSearch className="h-3 w-3" />查看 Graph
        </button>
        <div className="mt-1.5"><StatusBadge status={item.status} /></div>
      </td>
    </tr>
  )
}

function Metric({ icon: Icon, label, value, detail }: { icon: typeof Layers3; label: string; value: string; detail: string }) {
  return (
    <div className="rounded-lg border border-border bg-base p-3">
      <div className="flex items-center justify-between text-[11px] text-muted"><span>{label}</span><Icon className="h-3.5 w-3.5" /></div>
      <div className="mt-2 font-mono text-base font-semibold tabular-nums text-foreground">{value}</div>
      <div className="mt-1 text-[10px] text-muted">{detail}</div>
    </div>
  )
}

function PendingPanel({ label }: { label: string }) {
  return <div className="flex items-center gap-2 rounded-lg border border-dashed border-border p-4 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" />{label}</div>
}

function BlockedPanel({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-warning/25 bg-warning/5 p-3 text-xs text-warning">
      <Clock3 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div><div className="font-medium">尚未启动</div><div className="mt-1 text-[11px] leading-5 text-secondary">{message}。修复前置步骤后点击“重试失败项”，流程会从断点继续。</div></div>
    </div>
  )
}

function ErrorPanel({ message }: { message: string }) {
  return <div className="flex items-start gap-2 rounded-lg border border-danger/25 bg-danger/5 p-3 text-xs text-danger"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{message}</div>
}
