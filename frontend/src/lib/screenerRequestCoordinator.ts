export interface CoordinatedRequest {
  epoch: number
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
