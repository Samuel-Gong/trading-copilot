import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { Loader2, Search } from 'lucide-react'

import { boardTag } from '@/components/stock-table/primitives'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'

export type InstrumentSearchResult = {
  symbol: string
  name: string
  code: string
  asset_type?: string
}

type Props = {
  value: string
  onValueChange: (value: string) => void
  onSelect: (result: InstrumentSearchResult) => void
  assetTypes: string
  placeholder: string
  inputClassName: string
  menuClassName: string
  disabled?: boolean
  emptyText?: string
  inputId?: string
  renderTrailing?: (result: InstrumentSearchResult) => ReactNode
}

/**
 * 证券代码 / 名称联想输入框。
 *
 * 自选股和业务表单共享同一套查询、键盘导航与下拉关闭行为；业务侧只负责
 * 选中后的动作，以及可选的结果行尾部操作。
 */
export function InstrumentSearchInput({
  value,
  onValueChange,
  onSelect,
  assetTypes,
  placeholder,
  inputClassName,
  menuClassName,
  disabled = false,
  emptyText = '未找到匹配的标的',
  inputId,
  renderTrailing,
}: Props) {
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const generatedId = useId()
  const listboxId = `${generatedId}-instrument-results`
  const query = value.trim()

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, assetTypes),
    queryFn: () => api.instrumentSearch(query, 20, assetTypes),
    enabled: !disabled && query.length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []

  useEffect(() => {
    function closeOnOutsideClick(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }

    document.addEventListener('mousedown', closeOnOutsideClick)
    return () => document.removeEventListener('mousedown', closeOnOutsideClick)
  }, [])

  function selectResult(result: InstrumentSearchResult) {
    onSelect(result)
    setOpen(false)
    setActiveIndex(-1)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape' && open && query.length > 0) {
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
      setActiveIndex(-1)
      return
    }
    if (!open || results.length === 0) return

    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex(index => Math.min(index + 1, results.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex(index => Math.max(index - 1, -1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      selectResult(results[activeIndex >= 0 ? activeIndex : 0])
    }
  }

  const activeOptionId = activeIndex >= 0 ? `${listboxId}-${activeIndex}` : undefined

  return (
    <div ref={containerRef} className="relative">
      <div className="relative flex items-center">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open && query.length > 0}
          aria-controls={listboxId}
          aria-activedescendant={activeOptionId}
          autoComplete="off"
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          onChange={event => {
            onValueChange(event.target.value)
            setOpen(true)
            setActiveIndex(-1)
          }}
          onFocus={() => { if (query) setOpen(true) }}
          onKeyDown={handleKeyDown}
          className={cn(inputClassName, 'pl-8', search.isFetching && 'pr-8')}
        />
        {search.isFetching && (
          <Loader2 className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-muted" />
        )}
      </div>

      <AnimatePresence>
        {open && query.length > 0 && (
          <motion.div
            id={listboxId}
            role="listbox"
            aria-label="证券搜索结果"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              'absolute top-full z-[60] mt-1 max-h-[320px] overflow-y-auto rounded-card border border-border bg-base shadow-xl',
              menuClassName,
            )}
          >
            {search.isLoading ? (
              <div className="flex items-center justify-center gap-2 px-3 py-5 text-xs text-muted" role="status">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                搜索中…
              </div>
            ) : results.length === 0 ? (
              <div className="px-3 py-5 text-center text-xs text-muted" role="status">{emptyText}</div>
            ) : (
              results.map((result, index) => {
                const board = boardTag(result.symbol)
                return (
                  <div
                    key={result.symbol}
                    className={cn(
                      'flex items-center gap-2.5 px-3 py-2 text-xs transition-colors duration-100',
                      index === activeIndex ? 'bg-accent/10 text-accent' : 'text-foreground hover:bg-elevated',
                    )}
                    onMouseEnter={() => setActiveIndex(index)}
                  >
                    <button
                      id={`${listboxId}-${index}`}
                      type="button"
                      role="option"
                      aria-selected={index === activeIndex}
                      tabIndex={-1}
                      onMouseDown={event => event.preventDefault()}
                      onClick={() => selectResult(result)}
                      className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
                    >
                      <span className="w-[80px] shrink-0 font-mono">{result.symbol}</span>
                      <span className="min-w-0 flex-1 truncate text-secondary">{result.name}</span>
                      {result.asset_type === 'etf' && (
                        <span className="shrink-0 rounded bg-accent/10 px-1 py-0.5 text-[10px] leading-none text-accent">ETF</span>
                      )}
                      {result.asset_type === 'index' && (
                        <span className="shrink-0 rounded bg-sky-500/10 px-1 py-0.5 text-[10px] leading-none text-sky-400">指数</span>
                      )}
                      {board && (
                        <span className={cn('shrink-0 rounded border px-1 py-0.5 text-[10px] leading-none', board.color)}>{board.label}</span>
                      )}
                    </button>
                    {renderTrailing?.(result)}
                  </div>
                )
              })
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
