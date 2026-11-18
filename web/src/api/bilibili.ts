const base = '/api/v1/bilibili'
let token = ''
let deployment = ''
export function bindDeployment(id: string) { deployment = id }
let renewal: Promise<void> | undefined

export class LoginFailure extends Error {
  constructor(readonly code: string, message: string) { super(message) }
}
async function read(response: Response) {
  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    throw new LoginFailure(err.code || 'network', err.message || '账号服务未能响应')
  }
  return response
}
async function renew() {
  renewal ??= fetch(base + '/bootstrap', { cache: 'no-store', signal: AbortSignal.timeout(8000) })
    .then(read).then(r => r.json()).then(data => { token = data.token })
    .catch(e => { throw new LoginFailure('reconnect', e.message || '请重新连接本机账号服务') })
    .finally(() => { renewal = undefined })
  await renewal
}
export async function request(path: string, body?: unknown, key?: string, image = false) {
  if (!token) await renew()
  const send = (credential: string) => fetch(base + path, {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
    signal: AbortSignal.timeout(path.startsWith('/segment-uploads') ? 50000 : 15000),
    headers: { 'X-Mumu-Page': credential, ...(deployment && !path.startsWith('/segment-uploads') ? {'X-Mumu-Deployment':deployment} : {}), ...(body === undefined ? {} : {
      'Content-Type': 'application/json', 'Idempotency-Key': key!
    }) }, body: JSON.stringify(body),
  }).then(read)
  const original = token
  let res: Response
  try { res = await send(original) }
  catch (err) {
    if (!(err instanceof LoginFailure) || err.code !== 'page_expired') throw err
    if (token === original) await renew()
    if (body !== undefined || image) {
      throw new LoginFailure('recheck', '页面连接已更新，请核对原操作后继续')
    }
    try { res = await send(token) }
    catch (second) {
      if (second instanceof LoginFailure && second.code === 'page_expired') throw new LoginFailure('reconnect','请重新连接本机账号服务')
      throw second
    }
  }
  return image ? res.blob() : res.json()
}

export interface Intent { key: string; deployment: string; action: string; target: string | null }
const slot = 'mumu.account.operation'
export function pending(): Intent | null {
  const raw = sessionStorage.getItem(slot)
  return raw ? JSON.parse(raw) : null
}
export function remember(op: Intent) { sessionStorage.setItem(slot, JSON.stringify(op)) }
export function forget() { sessionStorage.removeItem(slot) }
