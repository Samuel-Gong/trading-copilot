import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import {
  Braces,
  Check,
  Clipboard,
  FileInput,
  FileOutput,
  Loader2,
  MessagesSquare,
  Network,
  Sparkles,
} from 'lucide-react'

import { cn } from '@/lib/cn'
import { api, type DailyReviewGraphDefinition, type DailyReviewGraphDefinitionNode } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const TEAM_COLORS: Record<string, string> = {
  context: '#64748B',
  analyst_team: '#2563EB',
  research_team: '#7C3AED',
  proposal: '#B86B05',
  risk_team: '#C43B6B',
  decision: '#0E7C66',
}

const EDGE_COLORS: Record<string, string> = {
  flow: '#7C8CA5',
  evidence: '#22A879',
  debate: '#A78BFA',
  feedback: '#F59E0B',
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

function edgeCurve(source: string, target: string, kind: string) {
  if (source === 'bear_researcher' && target === 'bull_researcher') return -0.26
  if (source === 'bull_researcher' && target === 'bear_researcher') return 0.18
  if (source === 'neutral_risk' && target === 'aggressive_risk') return -0.2
  return kind === 'feedback' ? 0.16 : 0.03
}

function buildTopologyOption(
  definition: DailyReviewGraphDefinition,
  selectedNodeId: string,
) {
  const nodes = new Map(definition.nodes.map(node => [node.id, node]))
  return {
    animation: false,
    grid: { left: 18, right: 18, top: 24, bottom: 24 },
    xAxis: { type: 'value', min: 0, max: 1230, show: false },
    yAxis: { type: 'value', min: 0, max: 600, inverse: true, show: false },
    tooltip: {
      trigger: 'item',
      formatter: (params: {
        seriesType?: string
        data?: DailyReviewGraphDefinitionNode & { source?: string; target?: string; edgeLabel?: string }
      }) => {
        if (params.seriesType === 'scatter') {
          return params.data ? `${params.data.label}<br/>点击查看配置` : ''
        }
        if (!params.data?.source || !params.data.target) return ''
        return `${nodes.get(params.data.source)?.label ?? params.data.source}<br/>→ ${nodes.get(params.data.target)?.label ?? params.data.target}<br/>${params.data.edgeLabel ?? ''}`
      },
    },
    series: [
      {
        name: 'edges',
        type: 'lines',
        coordinateSystem: 'cartesian2d',
        z: 2,
        symbol: ['none', 'arrow'],
        symbolSize: [0, 8],
        data: definition.edges.flatMap(edge => {
          const source = nodes.get(edge.source)
          const target = nodes.get(edge.target)
          if (!source || !target) return []
          return [{
            source: edge.source,
            target: edge.target,
            edgeLabel: edge.label,
            coords: [
              [source.position.x, source.position.y],
              [target.position.x, target.position.y],
            ],
            lineStyle: {
              color: EDGE_COLORS[edge.kind] ?? '#7C8CA5',
              width: edge.kind === 'feedback' ? 1.4 : 1.7,
              opacity: edge.kind === 'feedback' ? 0.72 : 0.62,
              type: edge.kind === 'feedback' ? 'dashed' : 'solid',
              curveness: edgeCurve(edge.source, edge.target, edge.kind),
            },
          }]
        }),
      },
      {
        name: 'nodes',
        type: 'scatter',
        coordinateSystem: 'cartesian2d',
        z: 5,
        data: definition.nodes.map(node => {
          const selected = node.id === selectedNodeId
          const color = TEAM_COLORS[node.team_id] ?? '#64748B'
          return {
            ...node,
            value: [node.position.x, node.position.y],
            symbol: 'roundRect',
            symbolSize: node.id === 'portfolio_manager' ? [148, 58] : [126, 52],
            itemStyle: {
              color,
              borderColor: selected ? '#F8FAFC' : color,
              borderWidth: selected ? 3.5 : 1.5,
              shadowBlur: selected ? 14 : 0,
              shadowColor: 'rgba(59,130,246,0.5)',
            },
            label: {
              show: true,
              color: '#F8FAFC',
              fontSize: 10.5,
              fontWeight: 600,
              lineHeight: 14,
              formatter: CHART_LABELS[node.id] ?? node.label,
            },
          }
        }),
        emphasis: { scale: 1.04 },
      },
    ],
  }
}

function PromptBlock({ label, content }: { label: string; content: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(content)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-base">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-elevated/60 px-3 py-2">
        <span className="text-[10px] font-semibold text-foreground">{label}</span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-[9px] text-muted hover:bg-surface hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3 text-success" /> : <Clipboard className="h-3 w-3" />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[10px] leading-5 text-secondary">
        {content}
      </pre>
    </div>
  )
}

export function SettingsAnalysisAgentsPanel() {
  const definitionQuery = useQuery({
    queryKey: QK.dailyReviewGraphDefinition,
    queryFn: api.dailyReviewGraphDefinition,
    staleTime: Number.POSITIVE_INFINITY,
  })
  const definition = definitionQuery.data
  const researchDebate = definition?.debates.research
  const [selectedNodeId, setSelectedNodeId] = useState('market_analyst')
  const selectedNode = definition?.nodes.find(node => node.id === selectedNodeId)
    ?? definition?.nodes[0]
  const selectedGroup = definition?.groups.find(group => group.id === selectedNode?.team_id)
  const option = useMemo(
    () => definition ? buildTopologyOption(definition, selectedNode?.id ?? '') : {},
    [definition, selectedNode?.id],
  )

  if (definitionQuery.isLoading) {
    return (
      <div className="grid min-h-[60vh] place-items-center rounded-card border border-border bg-surface">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </div>
    )
  }

  if (!definition || definitionQuery.isError) {
    return (
      <div className="rounded-card border border-danger/20 bg-danger/5 p-5 text-xs leading-5 text-danger">
        无法读取 Agent Graph 定义。请确认后端服务可用后重试。
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <section className="overflow-hidden rounded-card border border-border bg-surface">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Network className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-foreground">分析 Agent 拓扑</h2>
              <span className="rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 text-[9px] font-semibold text-accent">
                {definition.framework} · schema v{definition.schema_version}
              </span>
              <span className="rounded-full border border-border bg-base px-2 py-0.5 text-[9px] text-muted">
                静态定义
              </span>
              {researchDebate && (
                <span className="rounded-full border border-purple-400/25 bg-purple-400/10 px-2 py-0.5 text-[9px] font-semibold text-purple-300">
                  多空辩论 {researchDebate.max_rounds} 轮 · {researchDebate.max_turns} 次发言
                </span>
              )}
            </div>
            <p className="mt-1 max-w-3xl text-[11px] leading-5 text-muted">{definition.description}</p>
          </div>
          <div className="text-right text-[10px] leading-5 text-muted">
            <div>{definition.nodes.length} 个节点 · {definition.groups.length} 个阶段</div>
            <div className="text-success">Execution 已禁用</div>
          </div>
        </div>

        <div className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2.5">
          {definition.groups.map(group => (
            <span
              key={group.id}
              title={group.description}
              className="inline-flex items-center gap-1.5 rounded-full border border-border bg-base px-2.5 py-1 text-[9px] text-muted"
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: TEAM_COLORS[group.id] ?? '#64748B' }} />
              {group.label}
            </span>
          ))}
        </div>

        {researchDebate && (
          <div className="border-b border-border bg-[linear-gradient(90deg,rgba(124,58,237,0.08),transparent)] px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <MessagesSquare className="h-3.5 w-3.5 text-purple-400" />
                <span className="text-[10px] font-semibold text-foreground">{researchDebate.label}</span>
                <span className="text-[9px] text-muted">{researchDebate.stop_condition}</span>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-[9px]">
                {Array.from({ length: researchDebate.max_rounds }, (_, roundIndex) => (
                  <span key={roundIndex} className="inline-flex items-center gap-1.5">
                    <span className="rounded-full border border-success/25 bg-success/10 px-2 py-1 text-success">
                      第 {roundIndex + 1} 轮 Bull
                    </span>
                    <span className="text-muted">→</span>
                    <span className="rounded-full border border-danger/25 bg-danger/10 px-2 py-1 text-danger">
                      第 {roundIndex + 1} 轮 Bear
                    </span>
                    {roundIndex < researchDebate.max_rounds - 1 && <span className="text-muted">→</span>}
                  </span>
                ))}
                <span className="text-muted">→</span>
                <span className="rounded-full border border-purple-400/25 bg-purple-400/10 px-2 py-1 text-purple-300">
                  Research Manager
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="overflow-x-auto" aria-label="分析 Agent 静态拓扑图">
          <div className="min-w-[1240px] bg-[radial-gradient(circle_at_center,rgba(148,163,184,0.07)_1px,transparent_1px)] [background-size:18px_18px]">
            <ReactECharts
              option={option}
              notMerge
              style={{ height: 610, width: '100%' }}
              onEvents={{
                click: (params: { seriesType?: string; data?: { id?: string } }) => {
                  if (params.seriesType === 'scatter' && params.data?.id) setSelectedNodeId(params.data.id)
                },
              }}
              opts={{ renderer: 'canvas' }}
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 border-t border-border p-3 sm:grid-cols-4 xl:grid-cols-7" role="tablist" aria-label="Agent 节点列表">
          {definition.nodes.map(node => (
            <button
              key={node.id}
              type="button"
              role="tab"
              aria-selected={selectedNode?.id === node.id}
              onClick={() => setSelectedNodeId(node.id)}
              className={cn(
                'rounded-lg border px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                selectedNode?.id === node.id ? 'border-accent/45 bg-accent/10' : 'border-border bg-base hover:border-accent/25',
              )}
            >
              <span className="block truncate text-[10px] font-medium text-foreground">{node.label}</span>
              <span className="mt-1 block text-[9px] text-muted">{definition.groups.find(group => group.id === node.team_id)?.label}</span>
            </button>
          ))}
        </div>
      </section>

      {selectedNode && (
        <section className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold text-foreground">{selectedNode.label}</h2>
                <span className="rounded-full border border-border bg-base px-2 py-0.5 text-[9px] text-muted">{selectedGroup?.label}</span>
                <span className="rounded-full border border-border bg-base px-2 py-0.5 font-mono text-[9px] text-muted">{selectedNode.id}</span>
                {researchDebate?.participants.includes(selectedNode.id) && (
                  <span className="rounded-full border border-purple-400/25 bg-purple-400/10 px-2 py-0.5 text-[9px] text-purple-300">
                    每个目标调用 {researchDebate.max_rounds} 次
                  </span>
                )}
              </div>
              <p className="mt-1 max-w-3xl text-[11px] leading-5 text-muted">{selectedNode.description}</p>
            </div>
            <span className={cn(
              'rounded-full border px-2.5 py-1 text-[9px] font-medium',
              selectedNode.prompt.invokes_model
                ? 'border-accent/25 bg-accent/10 text-accent'
                : 'border-border bg-base text-muted',
            )}>
              {selectedNode.prompt.invokes_model ? '调用模型' : '确定性系统节点'}
            </span>
          </div>

          <div className="grid xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div className="min-w-0 border-b border-border p-4 xl:border-b-0 xl:border-r">
              <div className="mb-3 flex items-center gap-2">
                <FileInput className="h-3.5 w-3.5 text-accent" />
                <h3 className="text-xs font-semibold text-foreground">要求的输入</h3>
              </div>
              <div className="space-y-2">
                {selectedNode.required_inputs.map(input => (
                  <div key={input.id} className="rounded-lg border border-border bg-base px-3 py-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[10px] font-semibold text-foreground">{input.label}</span>
                      <span className={cn(
                        'rounded-full px-1.5 py-0.5 text-[8px] font-medium',
                        input.required ? 'bg-accent/10 text-accent' : 'bg-elevated text-muted',
                      )}>
                        {input.required ? '必需' : '可选'}
                      </span>
                    </div>
                    <p className="mt-1 text-[10px] leading-5 text-muted">{input.description}</p>
                    <div className="mt-1 font-mono text-[9px] text-muted">来源: {input.source}</div>
                  </div>
                ))}
              </div>
            </div>

            <div className="min-w-0 p-4">
              <div className="mb-3 flex items-center gap-2">
                <FileOutput className="h-3.5 w-3.5 text-success" />
                <h3 className="text-xs font-semibold text-foreground">要求的输出</h3>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {selectedNode.required_outputs.map((output, index) => (
                  <div key={output.id} className="rounded-lg border border-border bg-base px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="grid h-5 w-5 place-items-center rounded-full bg-success/10 font-mono text-[8px] text-success">{index + 1}</span>
                      <span className="text-[10px] font-semibold text-foreground">{output.label}</span>
                    </div>
                    <div className="mt-1.5 font-mono text-[9px] text-muted">{output.format} · 必需</div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="border-t border-border bg-elevated/20 p-4">
            <div className="mb-3 flex items-center gap-2">
              <Braces className="h-3.5 w-3.5 text-purple-400" />
              <h3 className="text-xs font-semibold text-foreground">Prompt</h3>
              <span className="text-[9px] text-muted">只读,与运行时使用同一份定义</span>
            </div>
            {selectedNode.prompt.invokes_model ? (
              <div className="grid gap-3 xl:grid-cols-2">
                <PromptBlock label="System Prompt" content={selectedNode.prompt.system} />
                <PromptBlock label="User Prompt 模板" content={selectedNode.prompt.user_template} />
              </div>
            ) : (
              <div className="flex items-start gap-2 rounded-lg border border-border bg-base p-4 text-[11px] leading-5 text-muted">
                <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                这是确定性事实装配节点,不会调用模型,因此没有 Prompt。它只从本地仓库读取并冻结输入。
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  )
}
