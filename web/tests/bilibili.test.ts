import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })

it('凭证过期时并发读请求共用一次续领，修改请求交给原操作恢复', async () => {
  let issued = 0, mutations = 0
  const fetcher = vi.fn(async (url:string, options:RequestInit) => {
    if (url.endsWith('/bootstrap')) return Response.json({token:'page'+ ++issued})
    if (options.method === 'POST') mutations++
    if ((options.headers as Record<string,string>)['X-Mumu-Page'] === 'page1') {
      return Response.json({code:'page_expired',message:'请续领'}, {status:401})
    }
    return Response.json({ok:true})
  })
  vi.stubGlobal('fetch',fetcher)
  const {request}=await import('../src/api/bilibili')
  const results=await Promise.allSettled([request('/accounts'),request('/sessions'),request('/operations',{},'original')])
  expect(results.map(r=>r.status)).toEqual(['fulfilled','fulfilled','rejected'])
  expect(issued).toBe(2)
  expect(mutations).toBe(1)
})

it('来源拒绝直接反馈给页面', async()=>{
  const fetcher=vi.fn(async(url:string)=>url.endsWith('/bootstrap')?Response.json({token:'local'}):
    Response.json({code:'local_only',message:'请在本机打开'},{status:403}))
  vi.stubGlobal('fetch',fetcher)
  const {request}=await import('../src/api/bilibili')
  await expect(request('/accounts')).rejects.toMatchObject({code:'local_only'})
  expect(fetcher).toHaveBeenCalledTimes(2)
})
