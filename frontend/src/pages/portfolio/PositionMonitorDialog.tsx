import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BellRing, ExternalLink, Layers3, Loader2, ShieldAlert, X } from 'lucide-react'

import { toast } from '@/components/Toast'
import { api, type PortfolioPosition, type PortfolioPriceMonitor } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { usePreferences } from '@/lib/useSharedQueries'

interface Props {
  position: PortfolioPosition
  monitor?: PortfolioPriceMonitor
  onClose: () => void
}

interface PriceInputProps {
  id: string
  value: string
  placeholder: string
  tone: 'danger' | 'warning'
  autoFocus?: boolean
  onChange: (value: string) => void
  onClear?: () => void
}

const PRICE_INPUT_TONES = {
  danger: {
    field: 'border-danger/30 focus-within:border-danger/60 focus-within:ring-danger/10',
    prefix: 'border-danger/20 bg-danger/[0.07] text-danger',
  },
  warning: {
    field: 'border-warning/30 focus-within:border-warning/60 focus-within:ring-warning/10',
    prefix: 'border-warning/20 bg-warning/[0.07] text-warning',
  },
} as const

function PriceInput({ id, value, placeholder, tone, autoFocus, onChange, onClear }: PriceInputProps) {
  const styles = PRICE_INPUT_TONES[tone]

  return (
    <div
      data-price-field={tone}
      className={`mt-2 flex h-10 overflow-hidden rounded-btn border bg-elevated/70 transition-[border-color,box-shadow,background-color] focus-within:bg-elevated focus-within:ring-2 ${styles.field}`}
    >
      <span
        aria-hidden="true"
        className={`grid w-9 shrink-0 place-items-center border-r font-mono text-xs ${styles.prefix}`}
      >
        ¥
      </span>
      <input
        id={id}
        autoFocus={autoFocus}
        type="number"
        inputMode="decimal"
        min="0"
        step="0.001"
        value={value}
        onChange={event => onChange(event.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 appearance-none bg-transparent px-3 font-mono text-sm text-foreground outline-none placeholder:font-sans placeholder:text-xs placeholder:text-muted/60 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
      />
      {value && onClear && (
        <button
          type="button"
          onClick={onClear}
          className="shrink-0 border-l border-warning/15 px-3 text-[10px] text-muted transition-colors hover:bg-warning/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-warning/20"
        >
          清空
        </button>
      )}
    </div>
  )
}

export function PositionMonitorDialog({ position, monitor, onClose }: Props) {
  const queryClient = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)
  const { data: prefs } = usePreferences()
  const [stopLossPrice, setStopLossPrice] = useState(
    monitor?.stop_loss_price == null ? '' : String(monitor.stop_loss_price),
  )
  const [addPositionPrice, setAddPositionPrice] = useState(
    monitor?.add_position_price == null ? '' : String(monitor.add_position_price),
  )
  const [channels, setChannels] = useState<string[]>(monitor?.webhook_channels ?? [])
  const channelsInitialized = useRef(Boolean(monitor))

  useEffect(() => {
    if (channelsInitialized.current || !prefs) return
    channelsInitialized.current = true
    const configured = new Set<string>()
    if (prefs.feishu_webhook_url) configured.add('feishu')
    if (prefs.wecom_webhook_url) configured.add('wecom')
    setChannels((prefs.webhook_default_channels ?? []).filter(channel => configured.has(channel)))
  }, [prefs])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  const stopValue = Number(stopLossPrice)
  const addValue = addPositionPrice.trim() === '' ? null : Number(addPositionPrice)
  const stopValid = Number.isFinite(stopValue) && stopValue > 0
  const addValid = addValue == null || (Number.isFinite(addValue) && addValue > stopValue)
  const save = useMutation({
    mutationFn: () => api.portfolioPriceMonitorSave(position.symbol, {
      name: position.name,
      asset_type: position.asset_type,
      stop_loss_price: stopValue,
      add_position_price: addValue,
      webhook_channels: channels,
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: QK.portfolioPriceMonitors })
      toast('持仓价格监控已保存', 'success')
      onClose()
    },
    onError: error => toast(error instanceof Error ? error.message : '保存价格监控失败', 'error'),
  })

  const toggleChannel = (channel: string) => {
    setChannels(current => current.includes(channel)
      ? current.filter(item => item !== channel)
      : [...current, channel])
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-3 backdrop-blur-sm sm:p-4" {...backdrop}>
      <div role="dialog" aria-modal="true" aria-labelledby="position-monitor-title" className="flex max-h-[90vh] w-full max-w-xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl" onClick={event => event.stopPropagation()}>
        <header className="flex items-center gap-3 border-b border-border px-4 py-3.5 sm:px-5">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-btn border border-danger/25 bg-danger/10 text-danger">
            <ShieldAlert className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 id="position-monitor-title" className="text-sm font-semibold text-foreground">持仓价格监控</h2>
            <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[11px] text-muted">
              <span className="truncate text-secondary">{position.name || position.symbol}</span>
              <span className="shrink-0 font-mono">{position.symbol}</span>
              <span className="shrink-0">分时最新价触发</span>
            </div>
          </div>
          <button onClick={onClose} className="rounded-btn p-1.5 text-muted hover:bg-elevated hover:text-foreground" title="关闭"><X className="h-4 w-4" /></button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 sm:p-5">
          <p className="text-xs leading-5 text-secondary">止损价和加仓价都按连续竞价时段的分时最新价判断。持仓表中的估值收盘价只用于市值和浮盈计算，不参与监控触发。</p>

          <section className="rounded-card border border-danger/30 bg-danger/[0.04] p-4">
            <div className="flex items-start gap-3">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="portfolio-stop-loss" className="text-xs font-medium text-foreground">分时止损价 <span className="text-danger">必填</span></label>
                </div>
                <PriceInput
                  id="portfolio-stop-loss"
                  autoFocus
                  value={stopLossPrice}
                  onChange={setStopLossPrice}
                  placeholder="输入止损价"
                  tone="danger"
                />
                <p className="mt-2 text-[10px] text-muted">触发条件：分时最新价 ≤ 止损价；按重要告警处理。</p>
              </div>
            </div>
          </section>

          <section className="rounded-card border border-warning/25 bg-warning/[0.04] p-4">
            <div className="flex items-start gap-3">
              <Layers3 className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="portfolio-add-position" className="text-xs font-medium text-foreground">分时加仓观察价 <span className="font-normal text-muted">可选</span></label>
                </div>
                <PriceInput
                  id="portfolio-add-position"
                  value={addPositionPrice}
                  onChange={setAddPositionPrice}
                  onClear={() => setAddPositionPrice('')}
                  placeholder="不需要时留空"
                  tone="warning"
                />
                <p className="mt-2 text-[10px] text-muted">触发条件：分时最新价 ≤ 加仓价；加仓价必须高于止损价。</p>
              </div>
            </div>
          </section>

          {!addValid && <div className="rounded-btn border border-danger/25 bg-danger/5 px-3 py-2 text-[11px] text-danger">加仓价必须高于止损价，避免两条规则的区间倒置。</div>}

          <section className="border-t border-border pt-4">
            <div className="text-[11px] text-muted">通知渠道</div>
            <div className="mt-2 flex flex-wrap gap-4">
              <label className="inline-flex items-center gap-2 text-xs text-foreground"><input type="checkbox" checked disabled className="h-3.5 w-3.5 accent-danger" />站内</label>
              {([
                { key: 'feishu', label: '飞书', configured: Boolean(prefs?.feishu_webhook_url) },
                { key: 'wecom', label: '企业微信', configured: Boolean(prefs?.wecom_webhook_url) },
              ]).map(channel => (
                <label key={channel.key} className={`inline-flex items-center gap-2 text-xs ${channel.configured ? 'text-foreground' : 'text-muted/60'}`}>
                  <input type="checkbox" checked={channels.includes(channel.key)} disabled={!channel.configured} onChange={() => toggleChannel(channel.key)} className="h-3.5 w-3.5 accent-danger" />
                  {channel.label}{!channel.configured && <span className="text-[9px]">未配置</span>}
                </label>
              ))}
            </div>
          </section>
        </div>

        <footer className="flex min-h-14 items-center justify-between gap-3 border-t border-border bg-base/30 px-4 py-3 sm:px-5">
          <Link to="/monitor" onClick={onClose} className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-accent">监控中心<ExternalLink className="h-3 w-3" /></Link>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="h-8 rounded-btn border border-border px-3 text-xs text-secondary hover:text-foreground">取消</button>
            <button onClick={() => save.mutate()} disabled={!stopValid || !addValid || save.isPending} className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-danger px-4 text-xs font-medium text-white hover:bg-danger/90 disabled:cursor-not-allowed disabled:opacity-40">
              {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BellRing className="h-3.5 w-3.5" />}
              保存监控
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
