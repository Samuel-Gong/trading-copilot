export interface CoordinatedRequest {
  epoch: number
}

export interface ScreenerRequestContext {
  asOf: string
  assetType: 'stock' | 'etf'
  version: number
}

export interface ScreenerRequestInput {
  date?: string
  assetType?: 'stock' | 'etf'
}

export function bindScreenerRequestContext<T extends ScreenerRequestInput>(
  request: T,
  context: ScreenerRequestContext,
) {
  const date = request.date ?? context.asOf
  const assetType = request.assetType ?? context.assetType
  if (date !== context.asOf || assetType !== context.assetType) return null
  return { ...request, date, assetType, context }
}

export function createScreenerRequestContext(
  initial: Omit<ScreenerRequestContext, 'version'>,
) {
  let current: ScreenerRequestContext = { ...initial, version: 0 }

  return {
    current: () => ({ ...current }),
    invalidate: (next: Partial<Omit<ScreenerRequestContext, 'version'>> = {}) => {
      current = { ...current, ...next, version: current.version + 1 }
      return { ...current }
    },
    matches: (context: ScreenerRequestContext) => (
      context.asOf === current.asOf
      && context.assetType === current.assetType
      && context.version === current.version
    ),
  }
}

/**
 * 串行化批量选股请求，并为每次结果上下文分配失效代际。
 *
 * 已在执行的请求不能可靠取消；当日期、资产、策略或配置变化时，
 * 调用 invalidate() 使旧响应失效。新的请求会替换队列中的旧请求，
 * 并在当前请求结束后立即执行。
 */
export function createScreenerRequestCoordinator<T extends object>() {
  let epoch = 0
  let pending = false
  let queued: T | null = null

  const startQueued = (request: T, start: (request: T & CoordinatedRequest) => void) => {
    pending = true
    start({ ...request, epoch } as T & CoordinatedRequest)
  }

  return {
    currentEpoch: () => epoch,
    isCurrent: (requestEpoch: number) => requestEpoch === epoch,
    invalidate: () => {
      epoch += 1
      queued = null
      return epoch
    },
    request: (request: T, start: (request: T & CoordinatedRequest) => void) => {
      if (pending) {
        queued = request
        return
      }
      startQueued(request, start)
    },
    settle: (start: (request: T & CoordinatedRequest) => void) => {
      pending = false
      const next = queued
      queued = null
      if (next) startQueued(next, start)
    },
  }
}
