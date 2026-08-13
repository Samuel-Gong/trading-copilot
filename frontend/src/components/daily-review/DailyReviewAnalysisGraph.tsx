import { useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  FileOutput,
  Loader2,
  MessagesSquare,
  RotateCcw,
  Route,
  Settings2,
} from 'lucide-react'

import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { cn } from '@/lib/cn'
import { DAILY_REVIEW_GRAPH_SCHEMA_VERSION } from '@/lib/api'
import type {
  DailyReviewAnalysisGraph,
  DailyReviewGraphDebate,
  DailyReviewGraphEdge,
  DailyReviewGraphEdgeStatus,
  DailyReviewGraphEvent,
  DailyReviewGraphNode,
  DailyReviewGraphNodeStatus,
} from '@/lib/api'

const STATUS_LABELS: Record<DailyReviewGraphNodeStatus, string> = {
  pending: '等待',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断',
  blocked: '被阻塞',
}

const STATUS_COLORS: Record<DailyReviewGraphNodeStatus, string> = {
  pending: '#3F4754',
  running: '#2563EB',
  completed: '#0E7C66',
  failed: '#C83C3C',
  interrupted: '#B86108',
  blocked: '#596273',
}

const EDGE_STYLES: Record<DailyReviewGraphEdgeStatus, {
  color: string
  width: number
  opacity: number
  type: 'solid' | 'dashed'
}> = {
  pending: { color: '#667085', width: 1.2, opacity: 0.28, type: 'dashed' },
  ready: { color: '#7C8CA5', width: 1.6, opacity: 0.68, type: 'solid' },
  active: { color: '#60A5FA', width: 2.4, opacity: 0.98, type: 'solid' },
  completed: { color: '#22A879', width: 1.8, opacity: 0.76, type: 'solid' },
  failed: { color: '#EF5B5B', width: 2.2, opacity: 0.92, type: 'solid' },
  blocked: { color: '#788395', width: 1.4, opacity: 0.38, type: 'dashed' },
}

const CHART_LABELS: Record<string, string> = {
  facts: '研究事实',
  market_analyst: 'Market\nAnalyst',
  sentiment_analyst: 'Sentiment\nAnalyst',
  news_analyst: 'News\nAnalyst',
  fundamentals_analyst: 'Fundamentals\nAnalyst',
  bull_researcher: 'Bull\nResearcher',
  bear_researcher: 'Bear\nResearcher',
  research_manager: 'Research\nManager',
  trader: 'Trader\n研究方案',
  aggressive_risk: 'Aggressive\nAnalyst',
  conservative_risk: 'Conservative\nAnalyst',
  neutral_risk: 'Neutral\nAnalyst',
  portfolio_manager: 'Portfolio Manager\n研究结论',
}

const TEAM_ACCENTS: Record<string, string> = {
  context: '#64748B',
  analyst_team: '#3B82F6',
  research_team: '#8B5CF6',
  proposal: '#D97706',
  risk_team: '#E0527D',
  decision: '#0E9F6E',
  legacy: '#71717A',
}

function NodeStatus({ status }: { status: DailyReviewGraphNodeStatus }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium',
        status === 'completed' && 'border-success/30 bg-success/10 text-success',
        status === 'running' && 'border-accent/30 bg-accent/10 text-accent',
        status === 'failed' && 'border-danger/30 bg-danger/5 text-danger',
        status === 'interrupted' && 'border-warning/30 bg-warning/5 text-warning',
        status === 'blocked' && 'border-border bg-elevated text-muted',
        status === 'pending' && 'border-border bg-elevated text-muted',
      )}
    >
      {status === 'running' && <Loader2 className="mr-1 h-2.5 w-2.5 animate-spin" />}
      {status === 'completed' && <CheckCircle2 className="mr-1 h-2.5 w-2.5" />}
      {STATUS_LABELS[status]}
    </span>
  )
}

function formatValue(value: unknown) {
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value)
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatTime(value: string | null | undefined) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN')
}

function edgeCurve(edge: DailyReviewGraphEdge) {
  if (edge.source === 'bear_researcher' && edge.target === 'bull_researcher') return -0.26
  if (edge.source === 'bull_researcher' && edge.target === 'bear_researcher') return 0.18
  if (edge.source === 'neutral_risk' && edge.target === 'aggressive_risk') return -0.2
  return edge.kind === 'feedback' ? 0.16 : 0.03
}

function eventDotClass(event: DailyReviewGraphEvent) {
  if (event.status === 'completed') return 'bg-success'
  if (event.status === 'running') return 'bg-accent'
  if (event.status === 'failed') return 'bg-danger'
  if (event.status === 'interrupted') return 'bg-warning'
  return 'bg-muted'
}

function DebateTranscript({ debate }: { debate: DailyReviewGraphDebate }) {
  return (
    <div className="border-b border-border bg-[linear-gradient(135deg,rgba(124,58,237,0.06),transparent_55%)] p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <MessagesSquare className="h-3.5 w-3.5 text-purple-400" />
            <h5 className="text-xs font-semibold text-foreground">完整多空辩论记录</h5>
            <span className="rounded-full border border-purple-400/25 bg-purple-400/10 px-2 py-0.5 text-[9px] font-medium text-purple-300">
              {debate.max_rounds} 轮 · {debate.max_turns} 次发言
            </span>
          </div>
          <p className="mt-1 text-[10px] leading-5 text-muted">
            Bull 与 Bear 读取对方上一轮观点后交替回应；全部发言完成后才进入 Research Manager。
          </p>
        </div>
        <div className="text-right text-[9px] leading-5 text-muted">
          <div>{STATUS_LABELS[debate.status]}</div>
          <div className="font-mono">{debate.completed_turns}/{debate.max_turns} 次有效发言</div>
        </div>
      </div>

      {debate.history.length > 0 ? (
        <div className="grid gap-2 xl:grid-cols-2">
          {debate.history.map((turn, index) => (
            <details
              key={turn.id}
              open={turn.status === 'failed' || turn.status === 'running' || index >= debate.history.length - 2}
              className={cn(
                'overflow-hidden rounded-lg border bg-base',
                turn.status === 'failed' ? 'border-danger/30' : 'border-border',
              )}
            >
              <summary className="cursor-pointer list-none px-3 py-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className={cn(
                      'rounded-full px-2 py-0.5 text-[9px] font-semibold',
                      turn.speaker_id === 'bull_researcher'
                        ? 'bg-success/10 text-success'
                        : 'bg-danger/10 text-danger',
                    )}>
                      第 {turn.round} 轮 · {turn.speaker_label}
                    </span>
                    <span className="font-mono text-[9px] text-muted">发言 {turn.turn} · 尝试 #{turn.attempt}</span>
                  </div>
                  <NodeStatus status={turn.status} />
                </div>
                {turn.output?.summary && (
                  <p className="mt-2 line-clamp-2 text-[10px] leading-5 text-secondary">{turn.output.summary}</p>
                )}
                {turn.error && <p className="mt-2 text-[10px] leading-5 text-danger">{turn.error}</p>}
              </summary>
              <div className="border-t border-border px-3 pb-3 pt-2.5">
                {turn.input?.previous_argument_summary && (
                  <div className="mb-3 rounded-md border border-border bg-surface px-2.5 py-2 text-[10px] leading-5 text-muted">
                    <span className="font-medium text-secondary">回应的上一轮观点：</span>
                    {turn.input.previous_argument_summary}
                  </div>
                )}
                {turn.output ? (
                  <MarkdownRenderer content={turn.output.markdown} />
                ) : (
                  <p className="text-[10px] leading-5 text-muted">
                    {turn.status === 'running' ? '本轮观点正在生成。' : '本次尝试没有生成有效输出。'}
                  </p>
                )}
              </div>
            </details>
          ))}
        </div>
      ) : (
        <p className="rounded-lg border border-dashed border-border bg-base p-3 text-[10px] leading-5 text-muted">
          辩论尚未开始。四类 Analyst 完成后，Bull 将发起第一轮论证。
        </p>
      )}
    </div>
  )
}

interface DailyReviewAnalysisGraphProps {
  graph: DailyReviewAnalysisGraph | null
  title: string
  subtitle: string
  retryingNodeId?: string | null
  onRetryNode: (node: DailyReviewGraphNode) => void
}

export function DailyReviewAnalysisGraphView({
  graph,
  title,
  subtitle,
  retryingNodeId,
  onRetryNode,
}: DailyReviewAnalysisGraphProps) {
  const [selectedNodeId, setSelectedNodeId] = useState('')
  const manualSelectionRef = useRef(false)
  const graphIdRef = useRef(graph?.id)
  const reduceMotion = useMemo(
    () => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    [],
  )

  useEffect(() => {
    if (!graph) return
    if (graphIdRef.current !== graph.id) {
      graphIdRef.current = graph.id
      manualSelectionRef.current = false
    }
    const currentExists = graph.nodes.some(node => node.id === selectedNodeId)
    if (manualSelectionRef.current && currentExists) return
    const preferred = graph.nodes.find(node => ['failed', 'interrupted'].includes(node.status))
      ?? graph.nodes.find(node => node.status === 'running')
      ?? graph.nodes.find(node => node.id === 'portfolio_manager' && node.status === 'completed')
      ?? graph.nodes.find(node => node.id === 'facts')
      ?? graph.nodes[0]
    setSelectedNodeId(preferred?.id ?? '')
  }, [graph, selectedNodeId])

  const selectedNode = graph?.nodes.find(node => node.id === selectedNodeId)
    ?? graph?.nodes.find(node => node.id === 'facts')
    ?? graph?.nodes[0]
  const selectedGroup = graph?.groups.find(group => group.id === selectedNode?.team_id)
  const researchDebate = graph?.debates?.research
  const showsResearchDebate = Boolean(
    selectedNode && ['bull_researcher', 'bear_researcher', 'research_manager'].includes(selectedNode.id),
  )
  const selectedEvents = useMemo(
    () => (graph?.events ?? [])
      .filter(event => event.node_id === selectedNode?.id)
      .slice()
      .reverse()
      .slice(0, 8),
    [graph?.events, selectedNode?.id],
  )

  const option = useMemo(() => {
    if (!graph || graph.schema_version < DAILY_REVIEW_GRAPH_SCHEMA_VERSION) return {}
    const nodesById = new Map(graph.nodes.map(node => [node.id, node]))
    const lineSeries = (Object.keys(EDGE_STYLES) as DailyReviewGraphEdgeStatus[])
      .map(status => {
        const style = EDGE_STYLES[status]
        const data = graph.edges
          .filter(edge => edge.status === status)
          .flatMap(edge => {
            const source = nodesById.get(edge.source)
            const target = nodesById.get(edge.target)
            if (!source?.position || !target?.position) return []
            return [{
              ...edge,
              coords: [
                [source.position.x, source.position.y],
                [target.position.x, target.position.y],
              ],
              lineStyle: {
                color: style.color,
                width: style.width,
                opacity: style.opacity,
                type: style.type,
                curveness: edgeCurve(edge),
              },
            }]
          })
        if (!data.length) return null
        return {
          name: status,
          type: 'lines',
          coordinateSystem: 'cartesian2d',
          z: status === 'active' ? 4 : 2,
          silent: false,
          symbol: ['none', 'arrow'],
          symbolSize: [0, status === 'active' ? 10 : 8],
          effect: {
            show: status === 'active' && !reduceMotion,
            period: 2.1,
            trailLength: 0.42,
            symbol: 'circle',
            symbolSize: 6,
            color: '#BFDBFE',
          },
          data,
        }
      })
      .filter(Boolean)

    return {
      animation: !reduceMotion,
      animationDurationUpdate: 320,
      grid: { left: 18, right: 18, top: 24, bottom: 24 },
      xAxis: { type: 'value', min: 0, max: 1230, show: false },
      yAxis: { type: 'value', min: 0, max: 600, inverse: true, show: false },
      tooltip: {
        trigger: 'item',
        borderWidth: 1,
        formatter: (params: {
          seriesType?: string
          data?: DailyReviewGraphNode & Partial<DailyReviewGraphEdge>
        }) => {
          if (params.seriesType === 'scatter') {
            const node = params.data as DailyReviewGraphNode | undefined
            return node ? `${node.label}<br/>${STATUS_LABELS[node.status]}` : ''
          }
          const edge = params.data as DailyReviewGraphEdge | undefined
          if (!edge?.source || !edge?.target) return ''
          return `${nodesById.get(edge.source)?.label ?? edge.source}<br/>→ ${nodesById.get(edge.target)?.label ?? edge.target}<br/>${edge.label}`
        },
      },
      series: [
        ...lineSeries,
        {
          name: 'nodes',
          type: 'scatter',
          coordinateSystem: 'cartesian2d',
          z: 6,
          data: graph.nodes.flatMap(node => {
            const selected = node.id === selectedNode?.id
            return [{
              ...node,
              value: [node.position.x, node.position.y],
              symbol: 'roundRect',
              symbolSize: node.id === 'portfolio_manager' ? [148, 58] : [126, 52],
              itemStyle: {
                color: STATUS_COLORS[node.status],
                borderColor: selected ? '#F8FAFC' : (TEAM_ACCENTS[node.team_id] ?? '#64748B'),
                borderWidth: selected ? 3.5 : 1.5,
                opacity: node.status === 'blocked' ? 0.74 : 0.96,
                shadowBlur: node.status === 'running' ? 18 : selected ? 10 : 0,
                shadowColor: node.status === 'running' ? '#60A5FA' : 'rgba(15,23,42,0.4)',
              },
              label: {
                show: true,
                color: '#F8FAFC',
                fontSize: node.id === 'portfolio_manager' ? 10 : 10.5,
                fontWeight: 600,
                lineHeight: 14,
                formatter: CHART_LABELS[node.id] ?? node.label,
              },
            }]
          }),
          emphasis: { scale: 1.04 },
        },
      ],
    }
  }, [graph, reduceMotion, selectedNode?.id])

  const selectNode = (nodeId: string) => {
    manualSelectionRef.current = true
    setSelectedNodeId(nodeId)
  }

  if (!graph) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-base p-5 text-xs leading-5 text-muted">
        这是一份旧版分析档案，没有可观察、可恢复的研究 Graph。点击页面顶部的“升级旧版复盘”后会生成新拓扑。
      </div>
    )
  }

  if (graph.schema_version < DAILY_REVIEW_GRAPH_SCHEMA_VERSION) {
    return (
      <div className="rounded-xl border border-warning/25 bg-warning/5 p-5">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            <h3 className="text-sm font-semibold text-foreground">旧版分析拓扑</h3>
            <p className="mt-1 text-xs leading-5 text-secondary">
              这份档案还不是当前研究 Graph。升级后才能看到完整拓扑、动态数据流和节点结构化输入输出。
            </p>
          </div>
        </div>
      </div>
    )
  }

  const progress = graph.progress
  const activeLabels = graph.nodes
    .filter(node => progress.active_node_ids.includes(node.id))
    .map(node => node.label)

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-base">
      <div className="border-b border-border px-4 py-3.5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Route className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-semibold text-foreground">{title}</h3>
              <span className="rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 text-[9px] font-semibold tracking-wide text-accent">
                TradingAgents · 研究模式
              </span>
              <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-[9px] text-muted">
                不连接 Execution
              </span>
              <a
                href="/settings?tab=analysis-agents"
                className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[9px] text-muted hover:border-accent/30 hover:text-accent"
              >
                <Settings2 className="h-2.5 w-2.5" />
                查看 Agent 配置
              </a>
            </div>
            <p className="mt-1 text-[11px] text-muted">{subtitle}</p>
          </div>
          <div className="min-w-48 text-right">
            <div className="flex items-center justify-end gap-2 text-[10px] text-secondary">
              <span>{progress.current_stage}</span>
              <span className="font-mono text-foreground">{progress.completed}/{progress.total}</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-elevated">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-500"
                style={{ width: `${Math.max(0, Math.min(100, progress.percent))}%` }}
              />
            </div>
            <div className="mt-1 font-mono text-[9px] text-muted">{progress.percent}% · {graph.id}</div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-1.5" aria-label="分析阶段">
          {graph.groups.map(group => {
            const current = group.id === progress.current_team_id
            const containsActiveNode = graph.nodes.some(node => node.team_id === group.id && node.status === 'running')
            return (
              <span
                key={group.id}
                title={group.description}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[9px] transition-colors',
                  current ? 'border-accent/35 bg-accent/10 text-accent' : 'border-border bg-surface text-muted',
                )}
              >
                <span
                  className={cn('h-1.5 w-1.5 rounded-full', containsActiveNode && !reduceMotion && 'animate-pulse')}
                  style={{ backgroundColor: TEAM_ACCENTS[group.id] ?? '#64748B' }}
                />
                {group.label}
              </span>
            )
          })}
        </div>

        {activeLabels.length > 0 && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-accent/20 bg-accent/5 px-3 py-2 text-[10px] text-secondary">
            <Activity className="h-3.5 w-3.5 shrink-0 text-accent" />
            <span>当前数据正在流向 <strong className="font-medium text-foreground">{activeLabels.join('、')}</strong></span>
          </div>
        )}
        {researchDebate
          && (progress.current_team_id === 'research_team'
            || ['failed', 'interrupted'].includes(researchDebate.status)) && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-purple-400/20 bg-purple-400/5 px-3 py-2 text-[10px] text-secondary">
            <span className="inline-flex items-center gap-2">
              <MessagesSquare className="h-3.5 w-3.5 shrink-0 text-purple-400" />
              多空辩论第 <strong className="font-medium text-foreground">{researchDebate.current_round}/{researchDebate.max_rounds}</strong> 轮
              {researchDebate.current_speaker_id && (
                <> · 当前发言：<strong className="font-medium text-foreground">
                  {graph.nodes.find(node => node.id === researchDebate.current_speaker_id)?.label ?? researchDebate.current_speaker_id}
                </strong></>
              )}
            </span>
            <span className="font-mono text-[9px] text-muted">{researchDebate.completed_turns}/{researchDebate.max_turns} 次有效发言</span>
          </div>
        )}
      </div>

      <div className="border-b border-border">
        <div className="overflow-x-auto" aria-label="TradingAgents 分析拓扑图">
          <div className="min-w-[1240px] bg-[radial-gradient(circle_at_center,rgba(148,163,184,0.07)_1px,transparent_1px)] [background-size:18px_18px]">
            <ReactECharts
              option={option}
              notMerge
              lazyUpdate
              style={{ height: 610, width: '100%' }}
              onEvents={{
                click: (params: { seriesType?: string; data?: { id?: string } }) => {
                  if (params.seriesType === 'scatter' && params.data?.id) selectNode(params.data.id)
                },
              }}
              opts={{ renderer: 'canvas' }}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border bg-surface/60 px-4 py-2.5 text-[9px] text-muted">
          {(Object.keys(EDGE_STYLES) as DailyReviewGraphEdgeStatus[]).map(status => (
            <span key={status} className="inline-flex items-center gap-1.5">
              <span
                className={cn('h-px w-5', EDGE_STYLES[status].type === 'dashed' && 'border-t border-dashed bg-transparent')}
                style={EDGE_STYLES[status].type === 'dashed'
                  ? { borderColor: EDGE_STYLES[status].color }
                  : { backgroundColor: EDGE_STYLES[status].color }}
              />
              {{ pending: '等待', ready: '输入就绪', active: '数据流动中', completed: '已传递', failed: '传递失败', blocked: '被阻塞' }[status]}
            </span>
          ))}
          <span className="ml-auto">虚线回路表示讨论反馈,不作为恢复依赖</span>
        </div>

        <div className="grid grid-cols-2 gap-2 border-t border-border p-3 sm:grid-cols-4 xl:grid-cols-7" role="tablist" aria-label="分析节点列表">
          {graph.nodes.map(node => (
            <button
              key={node.id}
              type="button"
              role="tab"
              aria-selected={selectedNode?.id === node.id}
              onClick={() => selectNode(node.id)}
              className={cn(
                'rounded-lg border px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                selectedNode?.id === node.id ? 'border-accent/45 bg-accent/10' : 'border-border bg-surface hover:border-accent/25',
              )}
            >
              <span className="block truncate text-[10px] font-medium text-foreground">{node.label}</span>
              <span className="mt-1 flex items-center justify-between gap-1 text-[9px] text-muted">
                <span>{STATUS_LABELS[node.status]}</span>
                <span className="font-mono">
                  {node.turns?.length ? `${node.turns.length} 次发言` : `#${node.attempt || 0}`}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>

      {selectedNode && (
        <section aria-label={`${selectedNode.label} 节点详情`}>
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3.5">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-sm font-semibold text-foreground">{selectedNode.label}</h4>
                <NodeStatus status={selectedNode.status} />
                <span className="rounded-full border border-border bg-elevated px-2 py-0.5 text-[9px] text-muted">
                  {selectedGroup?.label ?? selectedNode.team_id}
                </span>
              </div>
              <p className="mt-1 max-w-3xl text-[11px] leading-5 text-muted">{selectedNode.description}</p>
            </div>
            <div className="text-right font-mono text-[9px] leading-5 text-muted">
              <div>第 {selectedNode.attempt || 0} 次执行</div>
              <div>{formatTime(selectedNode.started_at)} → {formatTime(selectedNode.completed_at)}</div>
            </div>
          </div>

          {selectedNode.error && (
            <div className="mx-4 mt-4 flex flex-wrap items-start justify-between gap-3 rounded-lg border border-danger/25 bg-danger/5 p-3 text-xs leading-5 text-danger">
              <div className="flex min-w-0 items-start gap-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{selectedNode.error}</span>
              </div>
              {['failed', 'interrupted'].includes(selectedNode.status) && (
                <button
                  type="button"
                  onClick={() => onRetryNode(selectedNode)}
                  disabled={retryingNodeId === selectedNode.id}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-[10px] font-medium text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {retryingNodeId === selectedNode.id
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <RotateCcw className="h-3.5 w-3.5" />}
                  从此节点恢复
                </button>
              )}
            </div>
          )}

          {researchDebate && showsResearchDebate && <DebateTranscript debate={researchDebate} />}

          <div className="grid xl:grid-cols-2">
            <div className="min-w-0 border-b border-border p-4 xl:border-b-0 xl:border-r">
              <div className="mb-3 flex items-center gap-2">
                <Database className="h-3.5 w-3.5 text-accent" />
                <h5 className="text-xs font-semibold text-foreground">收到的输入</h5>
              </div>
              {selectedNode.input ? (
                <div className="space-y-3">
                  <p className="rounded-lg border border-border bg-surface px-3 py-2.5 text-[11px] leading-5 text-secondary">
                    {selectedNode.input.summary}
                  </p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {selectedNode.input.fields.map(field => (
                      <div key={`${selectedNode.id}:${field.key}`} className="rounded-lg border border-border bg-surface px-3 py-2">
                        <div className="text-[9px] text-muted">{field.label}</div>
                        <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-5 text-foreground">{formatValue(field.value)}</div>
                      </div>
                    ))}
                  </div>
                  <details className="rounded-lg border border-border bg-surface px-3 py-2.5">
                    <summary className="cursor-pointer text-[10px] font-medium text-secondary">查看冻结事实摘要</summary>
                    <div className="mt-2 whitespace-pre-wrap break-words border-t border-border pt-2 text-[10px] leading-5 text-muted">
                      {selectedNode.input.facts_summary || '没有额外事实摘要。'}
                    </div>
                  </details>
                  <div>
                    <div className="mb-2 text-[10px] font-medium text-secondary">上游材料</div>
                    {selectedNode.input.upstream.length > 0 ? (
                      <div className="space-y-2">
                        {selectedNode.input.upstream.map(source => (
                          <button
                            key={source.node_id}
                            type="button"
                            onClick={() => selectNode(source.node_id)}
                            className="block w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-left hover:border-accent/30"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-[10px] font-medium text-foreground">{source.label}</span>
                              <span className="text-[9px] text-muted">{STATUS_LABELS[source.status]}</span>
                            </div>
                            <p className="mt-1 text-[10px] leading-5 text-muted">{source.summary || '该上游节点没有可展示的摘要。'}</p>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="rounded-lg border border-dashed border-border p-3 text-[10px] leading-5 text-muted">
                        该节点直接读取冻结事实,没有上游 Agent 输出。
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-border p-4 text-xs leading-5 text-muted">
                  {selectedNode.status === 'blocked'
                    ? '上游失败导致该节点尚未接收到输入。恢复失败节点后,数据会沿依赖边继续流动。'
                    : '节点尚未开始,结构化输入会在进入运行态时写入档案。'}
                </p>
              )}
            </div>

            <div className="min-w-0 border-b border-border p-4 xl:border-b-0">
              <div className="mb-3 flex items-center gap-2">
                <FileOutput className="h-3.5 w-3.5 text-success" />
                <h5 className="text-xs font-semibold text-foreground">生成的输出</h5>
              </div>
              {selectedNode.output ? (
                <div className="space-y-3">
                  <p className="rounded-lg border border-success/20 bg-success/5 px-3 py-2.5 text-[11px] leading-5 text-secondary">
                    {selectedNode.output.summary || '节点已经完成。'}
                  </p>
                  {selectedNode.output.sections.map((section, index) => (
                    <div key={`${section.title}:${index}`} className="overflow-hidden rounded-lg border border-border bg-surface">
                      <div className="border-b border-border bg-elevated/50 px-3 py-2 text-[10px] font-semibold text-foreground">
                        {section.title}
                      </div>
                      <div className="px-3 pb-3">
                        <MarkdownRenderer content={section.content || '本节没有额外内容。'} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-border p-4 text-xs leading-5 text-muted">
                  {selectedNode.status === 'blocked'
                    ? '节点被阻塞,尚未生成输出。'
                    : selectedNode.status === 'running'
                      ? 'Agent 正在生成结构化输出,完成后会自动出现在这里。'
                      : '节点尚未产生输出。'}
                </p>
              )}
            </div>
          </div>

          <div className="border-t border-border bg-surface/40 px-4 py-3.5">
            <div className="mb-3 flex items-center gap-2">
              <Activity className="h-3.5 w-3.5 text-accent" />
              <h5 className="text-xs font-semibold text-foreground">动态记录</h5>
              <span className="font-mono text-[9px] text-muted">最近 {selectedEvents.length} 条</span>
            </div>
            {selectedEvents.length > 0 ? (
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {selectedEvents.map(event => (
                  <div key={event.id} className="flex items-start gap-2 rounded-lg border border-border bg-base px-3 py-2.5">
                    <span className={cn('mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full', eventDotClass(event))} />
                    <div className="min-w-0">
                      <div className="text-[10px] leading-5 text-secondary">{event.message}</div>
                      <div className="mt-0.5 font-mono text-[9px] text-muted">#{event.sequence} · {formatTime(event.at)}</div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[10px] text-muted">该节点还没有运行记录。</p>
            )}
          </div>
        </section>
      )}
    </div>
  )
}
