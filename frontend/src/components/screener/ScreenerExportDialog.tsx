import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Copy, Download, RefreshCw, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, screenerExportPath } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface Props {
  asOf: string
  activeStrategy: string | null
  strategyNames: Record<string, string>
  pool: string[]
  onClose: () => void
}

const buttonClass = 'inline-flex items-center justify-center gap-1.5 h-8 px-3 rounded-btn border border-border text-xs text-secondary hover:text-accent hover:border-accent/50 disabled:opacity-50 disabled:cursor-not-allowed'

export function ScreenerExportDialog({ asOf, activeStrategy, strategyNames, pool, onClose }: Props) {
  const [scope, setScope] = useState(activeStrategy ? 'single' : 'pool')
  const strategyIds = scope === 'single' && activeStrategy ? [activeStrategy] : pool
  const ready = strategyIds.length > 0 && !!asOf
  const preview = useQuery({
    queryKey: QK.screenerExport(strategyIds, asOf),
    queryFn: () => api.screenerExport(strategyIds, asOf),
    enabled: ready,
    staleTime: 0,
    retry: false,
  })
  const download = useMutation({
    mutationFn: async (format: 'csv' | 'txt') => {
      const blob = await api.screenerExportFile(strategyIds, asOf, format)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `screener-${asOf}.${format}`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    },
    onSuccess: () => toast('选股结果已导出', 'success'),
  })
  // 自动接入地址省略日期，每天请求同一地址；文件下载固定为页面所选日期。
  const apiUrl = new URL(screenerExportPath(strategyIds), window.location.origin).href
  const error = download.error ?? preview.error
  const busy = preview.isFetching || download.isPending
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(apiUrl)
      toast('API 地址已复制', 'success')
    } catch {
      toast('无法访问剪贴板，请选中地址手动复制', 'error')
    }
  }

  return (
    <Modal onClose={onClose} ariaLabel="导出选股结果" panelClassName="w-[600px] max-h-[85vh] overflow-y-auto bg-surface border border-border rounded-card shadow-xl">
      <div className="flex items-center justify-between px-5 py-4 border-b border-border">
        <h2 className="text-sm font-medium text-foreground flex items-center gap-2"><Download className="h-4 w-4 text-accent" />导出选股结果</h2>
        <button onClick={onClose} aria-label="关闭导出" className="text-muted hover:text-foreground"><X className="h-4 w-4" /></button>
      </div>
      <div className="p-5 space-y-5 text-sm">
        <div className="flex items-center justify-between gap-3">
          <label className="flex items-center gap-3 text-secondary">导出范围
            <select aria-label="导出范围" value={scope} disabled={download.isPending} onChange={e => { setScope(e.target.value); download.reset() }} className="h-8 max-w-[300px] bg-elevated border border-border rounded-btn px-2 text-foreground">
              {activeStrategy && <option value="single">{strategyNames[activeStrategy] ?? activeStrategy}</option>}
              <option value="pool">策略池（{pool.length} 个策略）</option>
            </select>
          </label>
          <span className="text-xs text-muted num">{asOf || '未选择日期'}</span>
        </div>
        <p className="text-xs leading-6 text-muted">导出所选策略的完整当前命中结果，不含今日已失效股票。页面的临时筛选、排序和显示条数不影响导出。</p>
        <div className="rounded-btn border border-border bg-elevated/50 px-4 py-3 space-y-3">
          {!ready ? <p className="text-muted">请先选择日期并向策略池添加策略。</p>
            : busy ? <p role="status" className="text-muted">{download.isPending ? '正在生成文件…' : '正在读取选股结果…'}</p>
              : !error && preview.data && <p className="text-secondary">{preview.data.total ? <>可导出 <strong className="text-accent num">{preview.data.total}</strong> 只股票 · {Object.keys(preview.data.results).length} 个策略</> : '所选策略当日无命中，可导出空文件。'}</p>}
          {error && <div role="alert" className="text-danger text-xs space-y-2"><p>{error.message}</p><button className={buttonClass} disabled={busy} onClick={() => { download.reset(); preview.refetch() }}><RefreshCw className="h-3 w-3" />重试</button></div>}
          <div className="flex items-center gap-2">
            <button className={buttonClass} disabled={!ready || busy || !preview.data || preview.isError} onClick={() => download.mutate('csv')}><Download className="h-3.5 w-3.5" />下载 CSV</button>
            <button className={buttonClass} disabled={!ready || busy || !preview.data || preview.isError} onClick={() => download.mutate('txt')}><Download className="h-3.5 w-3.5" />下载代码 TXT</button>
          </div>
          <p className="text-xs text-muted leading-5">CSV 每个策略命中一行，保留策略名称、价格和评分；TXT 每只股票一行，跨策略去重，保留交易所后缀。</p>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between"><h3 className="text-sm text-foreground">供其他软件读取的 JSON API</h3><button className={buttonClass} disabled={!ready} onClick={copy}><Copy className="h-3 w-3" />复制地址</button></div>
          <textarea aria-label="JSON API 地址" readOnly value={ready ? apiUrl : ''} onFocus={e => e.target.select()} className="w-full h-20 resize-none p-3 text-xs font-mono bg-elevated border border-border rounded-btn text-secondary" />
          <p className="text-xs text-muted leading-6">每天选股完成后请求同一地址，读取最新结果中的 symbols 去重代码清单。设有访问密码时，先调用 POST /api/auth/login，再携带返回的 Cookie 请求。指定日期可追加 as_of=YYYY-MM-DD；日期不一致会明确报错。</p>
          <p className="text-xs text-muted">其他设备接入时，将地址中的 localhost 或 127.0.0.1 换成面板可访问的域名或 IP。</p>
        </div>
      </div>
    </Modal>
  )
}
