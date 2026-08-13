/**
 * 修改单笔交易的费用/税费。
 *
 * 手动输入即覆盖（cost_source 变为 manual）；留空保存则按费率配置重新估算
 * （cost_source 变为 estimated）。对话框只改费用口径,不动成交要素。
 */
import { useState } from 'react'
import { CircleDollarSign, Loader2, X } from 'lucide-react'
import { toast } from '@/components/Toast'
import { api, type PortfolioTrade } from '@/lib/api'

const INPUT_CLASS = 'w-full rounded-btn border border-border bg-elevated px-2.5 py-2 text-xs text-foreground outline-none focus:border-accent/60 disabled:opacity-60'

function fmtQty(value: number) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value)
}

export function TradeCostDialog({ trade, onClose, onSaved }: {
  trade: PortfolioTrade
  onClose: () => void
  onSaved: () => void | Promise<void>
}) {
  const [fee, setFee] = useState(String(trade.fee))
  const [tax, setTax] = useState(String(trade.tax))
  const [busy, setBusy] = useState<'estimate' | 'save' | null>(null)

  async function estimate() {
    if (busy) return
    setBusy('estimate')
    try {
      const result = await api.portfolioTradeEstimate({
        symbol: trade.symbol,
        side: trade.side,
        quantity: trade.quantity,
        price: trade.price,
      })
      setFee(result.fee.toFixed(2))
      setTax(result.tax.toFixed(2))
    } catch {
      // request 内已弹错误 toast
    } finally {
      setBusy(null)
    }
  }

  async function save() {
    if (busy) return
    const feeValue = fee.trim() === '' ? null : Number(fee)
    const taxValue = tax.trim() === '' ? null : Number(tax)
    if (
      (feeValue !== null && (!Number.isFinite(feeValue) || feeValue < 0))
      || (taxValue !== null && (!Number.isFinite(taxValue) || taxValue < 0))
    ) {
      toast('费用/税费需为不小于 0 的数字，留空表示按费率重新估算', 'error')
      return
    }
    setBusy('save')
    try {
      await api.portfolioTradeUpdateCost(trade.id, { fee: feeValue, tax: taxValue })
      toast('费用/税费已更新', 'success')
      await onSaved()
      onClose()
    } catch {
      toast('费用/税费更新失败', 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/55 p-4" onClick={onClose}>
      <div className="w-full max-w-sm rounded-card border border-border bg-surface shadow-2xl" onClick={event => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-medium">修改费用/税费</h2>
            <p className="mt-0.5 font-mono text-[11px] text-muted">
              {trade.trade_date} · {trade.name || trade.symbol} · {trade.side === 'buy' ? '买入' : '卖出'} · {fmtQty(trade.quantity)} × ¥ {trade.price}
            </p>
          </div>
          <button onClick={onClose} className="rounded-btn p-1 text-muted hover:bg-elevated hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>
        <div className="space-y-3 p-4">
          <div className="flex items-center justify-between rounded-btn border border-border bg-elevated/40 px-3 py-2">
            <span className="text-[11px] text-muted">按「设置 → 交易费率」重新估算本次费用/税费</span>
            <button onClick={estimate} disabled={busy !== null} className="flex h-7 shrink-0 items-center gap-1 rounded-btn border border-accent/25 bg-accent/5 px-2.5 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-40">
              {busy === 'estimate' ? <Loader2 className="h-3 w-3 animate-spin" /> : <CircleDollarSign className="h-3 w-3" />}
              估算
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label htmlFor="cost-fee" className="flex items-center justify-between text-xs text-secondary"><span>手续费</span><span className="text-[10px] text-muted">留空重新估算</span></label>
              <input id="cost-fee" type="number" min="0" step="any" value={fee} onChange={event => setFee(event.target.value)} placeholder="留空自动估算" className={`${INPUT_CLASS} font-mono`} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="cost-tax" className="flex items-center justify-between text-xs text-secondary"><span>税费</span><span className="text-[10px] text-muted">留空重新估算</span></label>
              <input id="cost-tax" type="number" min="0" step="any" value={tax} onChange={event => setTax(event.target.value)} placeholder="留空自动估算" className={`${INPUT_CLASS} font-mono`} />
            </div>
          </div>
          <p className="text-[11px] leading-relaxed text-muted">手动输入并保存后记为「手工」口径；月底交割单导入仍可覆盖为实际费用。</p>
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <button onClick={onClose} className="h-8 rounded-btn px-3 text-xs text-secondary hover:bg-elevated">取消</button>
          <button onClick={save} disabled={busy !== null} className="flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50">
            {busy === 'save' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </div>
      </div>
    </div>
  )
}
