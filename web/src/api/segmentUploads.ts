import type { QualityFlag, Segment } from './types'

export const CONSOLE_ORIGIN = 'http://127.0.0.1:8000'
export const PLATFORM_ORIGIN = 'http://127.0.0.1:8765'
export const onLocalConsole = () => window.location.origin === CONSOLE_ORIGIN
export const segmentKey = (row: Pick<Segment, 'job_id' | 'segment_index'>) => row.job_id + ':' + row.segment_index

export interface UploadAccount {
  id: string
  remote_user_id: string
  display_name: string
}

export interface Parameters {
  title: string
  description: string
  tags: string[]
  category_id?: number
  copyright: 1 | 2
  source: string
  publish_mode: 'public' | 'self_only'
}

export interface UploadContext {
  deployment: string
  accounts: UploadAccount[]
  filename: string
  duration_sec: number | null
  quality_flags: QualityFlag[]
  defaults: Parameters
  account_id?: string | null
}

export interface Preview {
  ticket: string
  expires_at: string
  work: Pick<Parameters, 'title' | 'description' | 'tags'>
  video: string
  options: Pick<Parameters, 'category_id' | 'copyright' | 'source' | 'publish_mode'>
  account: UploadAccount
  quality_flags: QualityFlag[]
  warnings: string[]
  media_url: string
}

export interface Arrangement {
  id: string
  work_id: string
  task_id: string | null
  job_id: string
  segment_index: number
  automatic: boolean
  rejected: boolean
  can_continue: boolean
  can_revise: boolean
  observed: string | null
  problem: string | null
}

export async function request<T>(path: string, body?: unknown): Promise<T> {
  if (!onLocalConsole()) throw new Error('请在录制机打开 http://127.0.0.1:8000')
  const response = await fetch('/api/uploads' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(45000),
    cache: 'no-store',
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.message || '本机投稿服务暂不可用')
  return data as T
}

const base = '/segment-uploads'
const partPath = (row: Segment) => '/' + encodeURIComponent(row.job_id) + '/' + row.segment_index
export const segmentUploads = {
  list(rows: Segment[]) {
    const query = new URLSearchParams()
    for (const row of rows) query.append('keys', segmentKey(row))
    return request<{items: Arrangement[]}>(base + '?' + query)
  },
  context(row: Segment, revise?: string) {
    return request<UploadContext>(base + '/context' + partPath(row) + (revise ? '?revise=' + encodeURIComponent(revise) : ''))
  },
  preview(row: Segment, deployment: string, account: UploadAccount, parameters: Parameters, revise?: string) {
    return request<Preview>(base + '/preview' + partPath(row), {
      deployment, account_id: account.id, remote_user_id: account.remote_user_id, parameters, revise,
    })
  },
  confirm(ticket: string, quality: boolean) {
    return request<Arrangement>(base + '/confirm/' + encodeURIComponent(ticket), { quality_confirmed: quality })
  },
  detail(id: string) { return request<Arrangement>(base + '/' + encodeURIComponent(id)) },
  resume(id: string) { return request<Arrangement>(base + '/' + encodeURIComponent(id) + '/continue', {}) },
}

export function arrangementStatus(record: Arrangement) {
  if (record.task_id) return record.observed === 'UNCERTAIN' ? '平台待核验' : '平台已受理'
  if (record.problem) return '待核对'
  return record.automatic ? '整场待交付' : '待交付'
}
