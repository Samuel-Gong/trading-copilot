export function buildBackendTarget(
  host: string | undefined,
  port: string | undefined,
  legacyPort?: string,
): string {
  const backendHost = host || '127.0.0.1'
  const proxyHost = ['0.0.0.0', '::'].includes(backendHost) ? '127.0.0.1' : backendHost
  const normalizedHost = proxyHost.includes(':')
    && !(proxyHost.startsWith('[') && proxyHost.endsWith(']'))
    ? `[${proxyHost}]`
    : proxyHost
  return `http://${normalizedHost}:${port || legacyPort || '3018'}`
}
