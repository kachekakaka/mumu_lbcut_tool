export type QueryValue = string | number | boolean | null | undefined
export type Query = Record<string, QueryValue>

export interface RequestOptions {
  query?: Query
  body?: unknown
  headers?: Record<string, string>
  timeoutMs?: number
  signal?: AbortSignal
}

export interface Response<T> {
  status: number
  data: T
}

export class ApiError extends Error {
  status: number
  code: string
  details: unknown

  constructor(status: number, code: string, message: string, details: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

/** 无响应、代理网关错误或服务未就绪：视为与后端断开。 */
export function isDisconnect(error: unknown): boolean {
  return error instanceof ApiError && ['network_error', 'service_unavailable', 'frontend_unavailable'].includes(error.code)
}

export function buildQuery(query?: Query): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.append(key, typeof value === 'boolean' ? (value ? 'true' : 'false') : String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<Response<T>> {
  if (__MOCK__) {
    const mock = await import('../mock')
    return mock.mockRequest<T>(method, path, options)
  }
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (options.signal?.aborted) abort()
  else options.signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 10000)
  let response: globalThis.Response
  let text: string
  try {
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    response = await fetch(path + buildQuery(options.query), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
      cache: 'no-store',
    })
    text = await response.text()
  } catch {
    throw new ApiError(0, 'network_error', '无法连接服务')
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener('abort', abort)
  }
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!response.ok) {
    const body = data as { code?: unknown; message?: unknown; details?: unknown } | null
    if (body && typeof body.code === 'string') {
      throw new ApiError(response.status, body.code, typeof body.message === 'string' ? body.message : '', body.details ?? null)
    }
    // 没有统一错误体的 5xx 来自开发代理或中间网络，按断开处理。
    throw new ApiError(response.status, response.status >= 500 ? 'network_error' : 'http_error', `HTTP ${response.status}`)
  }
  if (data === null) throw new ApiError(response.status, 'invalid_response', '服务返回内容无法解析')
  return { status: response.status, data: data as T }
}
