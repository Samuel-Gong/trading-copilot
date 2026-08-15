import { Binoculars } from 'lucide-react'

import { InstrumentSearchInput } from '@/components/InstrumentSearchInput'

const SEARCH_INPUT_CLASS = 'h-10 w-full rounded-btn border border-border bg-base pr-3 text-sm text-foreground outline-none transition-colors focus:border-accent/60 disabled:opacity-60'

type Props = {
  readonly query: string
  readonly disabled: boolean
  readonly onQueryChange: (value: string) => void
  readonly onSelect: (symbol: string) => void
}

export function WatchPoolAddSection({ query, disabled, onQueryChange, onSelect }: Props) {
  return (
    <section className="overflow-visible rounded-card border border-border bg-surface">
      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(280px,440px)_1fr] lg:items-center">
        <div>
          <label htmlFor="watch-pool-search" className="mb-2 block text-xs font-medium text-secondary">
            添加观察标的
          </label>
          <InstrumentSearchInput
            inputId="watch-pool-search"
            value={query}
            onValueChange={onQueryChange}
            onSelect={result => onSelect(result.symbol)}
            assetTypes="stock,etf"
            placeholder="输入股票代码或名称，如 600519 / 茅台"
            inputClassName={SEARCH_INPUT_CLASS}
            menuClassName="left-0 right-0"
            disabled={disabled}
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
  )
}
