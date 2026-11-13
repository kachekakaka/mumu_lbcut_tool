import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, buildQuery, isDisconnect, request } from '../src/api/client'
import { newRequestId } from '../src/ids'

afterEach(() => vi.unstubAllGlobals())

describe('接口客户端', () => {
  it('布尔参数只发送 true/false，空值不发送', () => {
    expect(buildQuery({ active: false, has_notice: true, q: '', status: undefined, limit: 50 })).toBe('?active=false&has_notice=true&limit=50')
  })

  it('统一错误体保留错误码；无错误体的网关错误与断网视为断开', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 'service_unavailable', message: '服务未就绪', details: null }), { status: 503 })))
    const e1 = await request('GET', '/api/system').catch((e) => e)
    expect(e1).toBeInstanceOf(ApiError)
    expect(e1.code).toBe('service_unavailable')
    expect(isDisconnect(e1)).toBe(true)

    vi.stubGlobal('fetch', vi.fn(async () => new Response('Bad Gateway', { status: 502 })))
    expect(isDisconnect(await request('GET', '/api/system').catch((e) => e))).toBe(true)

    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    const e3 = await request('GET', '/api/system').catch((e) => e)
    expect(e3.code).toBe('network_error')

    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 'invalid_parameters', message: 'x', details: null }), { status: 422 })))
    const e4 = await request('GET', '/api/jobs').catch((e) => e)
    expect(e4.code).toBe('invalid_parameters')
    expect(isDisconnect(e4)).toBe(false)
  })

  it('写请求带请求编号头', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ command_id: 'c1', status: 'pending' }), { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    const { api } = await import('../src/api')
    await api.stopTarget('t/1', 'session-1', 'req-1')
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/targets/t%2F1/stop')
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('req-1')
    expect(init.body).toBe(JSON.stringify({ session_key: 'session-1' }))

    await api.setInstanceEnabled(2, true, 'req-2')
    const [iUrl, iInit] = fetchMock.mock.calls[1] as unknown as [string, RequestInit]
    expect(iUrl).toBe('/api/instances/2')
    expect(iInit.method).toBe('PATCH')
    expect((iInit.headers as Record<string, string>)['Idempotency-Key']).toBe('req-2')
    expect(iInit.body).toBe(JSON.stringify({ enabled: true }))

    await api.instances({ status: 'booting', enabled: true })
    const [qUrl] = fetchMock.mock.calls[2] as unknown as [string, RequestInit]
    expect(qUrl).toBe('/api/instances?status=booting&enabled=true')
  })

  it('非安全上下文没有 randomUUID 时仍生成 UUID v4', () => {
    const original = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: (b: Uint8Array) => original.getRandomValues(b) })
    const id = newRequestId()
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })
})
