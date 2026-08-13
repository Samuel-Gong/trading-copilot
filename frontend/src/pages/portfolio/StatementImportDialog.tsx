/**
 * 交割单导入对话框 — 两阶段：上传解析 → 预览确认入库。
 *
 * 预览行 mode：insert=新增, calibrate=校准(覆盖估算费用), skip=跳过(不入库)。
 * 无法识别标的的行单独列出，不参与提交。
 */
import { useRef, useState } from 'react'
import { AlertTriangle, FileSpreadsheet, Loader2, Upload, X } from 'lucide-react'
import { toast } from '@/components/Toast'
import {
  api,
  type PortfolioAccount,
  type StatementCommitItem,
  type StatementPreviewItem,
  type StatementPreviewResponse,
} from '@/lib/api'
import { cn } from '@/lib/cn'

const MAX_FILE_BYTES = 12 * 1024 * 1024
const ACCEPT = '.csv,.xlsx,.xls'

function fmtMoney(value: number) {
  return new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}

function toCommitItem(item: StatementPreviewItem): StatementCommitItem {
  if (item.mode === 'calibrate') {
    return { mode: 'calibrate', matched_trade_id: item.matched_trade_id, fee: item.fee, tax: item.tax }
  }
  return {
    mode: 'insert',
    symbol: item.symbol,
    trade_date: item.trade_date,
    side: item.side,
    quantity: item.quantity,
    price: item.price,
    fee: item.fee,
    tax: item.tax,
  }
}

export function StatementImportDialog({ accounts, defaultAccountId, onClose, onCommitted }: {
  accounts: PortfolioAccount[]
  defaultAccountId: string
  onClose: () => void
  onCommitted: () => void | Promise<void>
}) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [accountId, setAccountId] = useState(defaultAccountId)
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState<'preview' | 'commit' | null>(null)
  const [preview, setPreview] = useState<StatementPreviewResponse | null>(null)

  const accountName = accounts.find(account => account.id === accountId)?.name ?? ''
  const insertCount = preview?.items.filter(item => item.mode === 'insert').length ?? 0
  const calibrateCount = preview?.items.filter(item => item.mode === 'calibrate').length ?? 0
  const skipCount = preview?.items.filter(item => item.mode === 'skip').length ?? 0

  function pickFile(next: File | null) {
    if (!next) {
      setFile(null)
      return
    }
    const suffix = next.name.slice(next.name.lastIndexOf('.')).toLowerCase()
    if (!ACCEPT.split(',').includes(suffix)) {
      toast('仅支持 csv / xlsx / xls 交割单', 'error')
      return
    }
    if (next.size > MAX_FILE_BYTES) {
      toast('文件超过 12MB 上限', 'error')
      return
    }
    setFile(next)
  }

  async function runPreview() {
    if (!file || !accountId || busy) return
    setBusy('preview')
    try {
      setPreview(await api.portfolioStatementPreview(accountId, file))
    } catch {
      // request 内已弹错误 toast
    } finally {
      setBusy(null)
    }
  }

  async function runCommit() {
    if (!preview || busy) return
    const items = preview.items.filter(item => item.mode !== 'skip').map(toCommitItem)
    if (items.length === 0) return
    setBusy('commit')
    try {
      const result = await api.portfolioStatementCommit({ account_id: accountId, items })
      toast(`导入完成：新增 ${result.inserted} 条，校准 ${result.calibrated} 条，跳过 ${result.skipped} 条`, 'success')
      await onCommitted()
      onClose()
    } catch {
      // request 内已弹错误 toast
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/55 p-4" onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-card border border-border bg-surface shadow-2xl" onClick={event => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-medium">导入交割单</h2>
            <p className="mt-0.5 text-[11px] text-muted">按账户自然键匹配已有交易：费用有出入的标记「校准」，其余新增；重复的自动跳过</p>
          </div>
          <button onClick={onClose} className="rounded-btn p-1 text-muted hover:bg-elevated hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        {preview === null ? (
          <div className="space-y-4 p-4">
            <div className="space-y-1.5">
              <label htmlFor="statement-account" className="flex items-center justify-between text-xs text-secondary"><span>账户</span></label>
              <select
                id="statement-account"
                value={accountId}
                onChange={event => setAccountId(event.target.value)}
                className="w-full rounded-btn border border-border bg-elevated px-2.5 py-2 text-xs text-foreground outline-none focus:border-accent/60 disabled:opacity-60"
              >
                {accounts.map(account => <option key={account.id} value={account.id}>{account.name}</option>)}
              </select>
            </div>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="flex w-full flex-col items-center gap-2 rounded-card border border-dashed border-border bg-elevated/40 px-4 py-8 text-muted hover:border-accent/50 hover:text-accent"
            >
              {file ? <FileSpreadsheet className="h-6 w-6" /> : <Upload className="h-6 w-6" />}
              <span className="text-xs">{file ? file.name : '选择券商交割单（csv / xlsx / xls，≤ 12MB）'}</span>
              {file && <span className="font-mono text-[10px] text-muted">{(file.size / 1024).toFixed(1)} KB</span>}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={event => {
                pickFile(event.target.files?.[0] ?? null)
                event.target.value = ''
              }}
            />
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="h-8 rounded-btn px-3 text-xs text-secondary hover:bg-elevated">取消</button>
              <button
                onClick={runPreview}
                disabled={!file || !accountId || busy !== null}
                className="flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"
              >
                {busy === 'preview' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                解析预览
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between border-b border-border/60 px-4 py-2 text-[11px] text-muted">
              <span>{accountName} · {file?.name}</span>
              <span>新增 <span className="font-mono text-bull">{insertCount}</span> · 校准 <span className="font-mono text-warning">{calibrateCount}</span> · 跳过 <span className="font-mono">{skipCount}</span></span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              {preview.items.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted">没有可入库的成交记录</div>
              ) : (
                <table className="w-full text-left text-xs">
                  <thead className="text-muted">
                    <tr>
                      <th className="py-1.5 pr-2 font-medium">日期 / 标的</th>
                      <th className="py-1.5 pr-2 font-medium">方向</th>
                      <th className="py-1.5 pr-2 text-right font-medium">数量 × 价格</th>
                      <th className="py-1.5 pr-2 text-right font-medium">费用 / 税费</th>
                      <th className="py-1.5 text-right font-medium">处理方式</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/70">
                    {preview.items.map((item, index) => (
                      <PreviewRow key={`${item.trade_date}-${item.symbol}-${index}`} item={item} />
                    ))}
                  </tbody>
                </table>
              )}
              {(preview.unresolved.length > 0 || preview.skipped_rows.length > 0) && (
                <div className="mt-3 rounded-btn border border-warning/25 bg-warning/5 px-3 py-2">
                  <div className="flex items-center gap-1.5 text-[11px] text-warning"><AlertTriangle className="h-3 w-3" />以下行无法识别，不会入库</div>
                  <ul className="mt-1 space-y-0.5 text-[11px] text-secondary">
                    {preview.unresolved.map((row, index) => (
                      <li key={`unresolved-${index}`}>{row.trade_date} · {row.raw_code} {row.name} — 未匹配到本地标的</li>
                    ))}
                    {preview.skipped_rows.map((row, index) => (
                      <li key={`skipped-${index}`}>第 {row.row} 行 — {row.reason}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
              <button
                onClick={() => setPreview(null)}
                disabled={busy !== null}
                className="h-8 rounded-btn px-3 text-xs text-secondary hover:bg-elevated disabled:opacity-50"
              >
                重新选择文件
              </button>
              <button
                onClick={runCommit}
                disabled={insertCount + calibrateCount === 0 || busy !== null}
                className="flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"
              >
                {busy === 'commit' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                确认导入（新增 {insertCount} · 校准 {calibrateCount}）
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function PreviewRow({ item }: { item: StatementPreviewItem }) {
  const buying = item.side === 'buy'
  return (
    <tr className={cn(item.mode === 'skip' && 'opacity-45')}>
      <td className="py-2 pr-2">
        <div>{item.name || item.symbol} <span className="font-mono text-[10px] text-muted">{item.symbol}</span></div>
        <div className="font-mono text-[10px] text-muted">{item.trade_date}</div>
      </td>
      <td className={cn('py-2 pr-2', buying ? 'text-bull' : 'text-bear')}>{buying ? '买入' : '卖出'}</td>
      <td className="py-2 pr-2 text-right font-mono">{item.quantity} × ¥ {fmtMoney(item.price)}</td>
      <td className="py-2 pr-2 text-right font-mono">
        ¥ {fmtMoney(item.fee)} / ¥ {fmtMoney(item.tax)}
        {item.mode === 'calibrate' && item.current_fee != null && item.current_tax != null && (
          <div className="text-[10px] text-muted">原 ¥ {fmtMoney(item.current_fee)} / ¥ {fmtMoney(item.current_tax)}</div>
        )}
      </td>
      <td className="py-2 text-right"><ModeChip mode={item.mode} /></td>
    </tr>
  )
}

function ModeChip({ mode }: { mode: StatementPreviewItem['mode'] }) {
  if (mode === 'insert') {
    return <span className="inline-flex items-center rounded-full border border-bull/20 bg-bull/5 px-2 py-0.5 text-[10px] text-bull">新增</span>
  }
  if (mode === 'calibrate') {
    return <span className="inline-flex items-center rounded-full border border-warning/20 bg-warning/5 px-2 py-0.5 text-[10px] text-warning">校准</span>
  }
  return <span className="inline-flex items-center rounded-full border border-border bg-elevated px-2 py-0.5 text-[10px] text-muted">跳过</span>
}
