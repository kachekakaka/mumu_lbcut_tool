import { request } from './bilibili'
import type { Segment } from './types'

export interface UploadAccount {
  id: string
  display_name: string
  remote_user_id: string | null
}

export interface PostingFields {
  title: string
  description: string
  tags: string[]
  category_id: number
  copyright: 1 | 2
  source: string
  publish_mode: 'public' | 'self_only'
}

export interface UploadContext {
  ticket: string
  deployment: string
  port: number
  accounts: UploadAccount[]
  worker_online: boolean
  publish_modes: PostingFields['publish_mode'][]
  limits: { title: number; description: number; tag: number; tag_count: number }
  nickname: string
  filename: string
  duration_sec: number | null
  quality_flags: Segment['quality_flags']
  defaults: Partial<PostingFields>
}

export interface Arrangement {
  id: string
  job_id: string
  segment_index: number
  stage: 'register' | 'submit' | 'task'
  revision: number
  task_id: string | null
  status: string | null
  worker_online: boolean | null
  observed_at: number | null
  problem: string | null
  rejected: boolean
  can_revise: boolean
  can_continue: boolean
}

export interface ArrangementDetail extends Arrangement {
  deployment: string
  work_id: string
  account: UploadAccount
  parameters: PostingFields
  filename: string
  bvid?: string
  aid?: number
  reason?: string
}

const path = '/segment-uploads'
export const onLocalConsole = () => ['127.0.0.1', '[::1]'].includes(window.location.hostname)
export const segmentKey = (row: Pick<Segment, 'job_id' | 'segment_index'>) => `${row.job_id}:${row.segment_index}`

export const segmentUploads = {
  list: (rows: Segment[]): Promise<{items: Arrangement[]}> => {
    const query = new URLSearchParams()
    for (const row of rows) query.append('keys', segmentKey(row))
    return request(`${path}?${query}`)
  },
  context: (row: Segment, revise?: string): Promise<UploadContext> =>
    request(`${path}/context/${row.job_id}/${row.segment_index}${revise ? `?revise=${revise}` : ''}`),
  confirm: (key: string, context: UploadContext, account: string, parameters: PostingFields, quality: boolean): Promise<ArrangementDetail> =>
    request(path, { ticket: context.ticket, account_id: account, parameters, quality_confirmed: quality }, key),
  detail: (id: string): Promise<ArrangementDetail> => request(`${path}/${id}`),
  resume: (row: Arrangement): Promise<ArrangementDetail> =>
    request(`${path}/${row.id}/continue`, { revision: row.revision }, crypto.randomUUID()),
}

const statuses: Record<string, string> = {
  QUEUED: '已排队', CLAIMED: '已领取', VALIDATING: '准备上传', RUNNING: '上传中',
  RETRY_WAIT: '等待上传模块重试', RECOVERY_PENDING: '上传模块恢复中', UNCERTAIN: '投稿结果待核对',
  SUCCEEDED: '投稿成功', FAILED: '失败', CANCELLED: '已取消', EXPIRED: '已过期',
}
export function arrangementStatus(row: Arrangement) {
  if (row.rejected) return 'Core 明确拒绝'
  if (row.stage === 'task') {
    if (row.status === 'QUEUED' && row.worker_online === false) return '已排队，等待 Worker'
    return row.status ? statuses[row.status] || `未知状态（${row.status}）` : '已关联，等待状态'
  }
  if (row.problem) return row.stage === 'register' ? '登记待确认' : '提交待确认'
  return row.stage === 'register' ? '已保存安排' : '已登记，待取得任务'
}

export const problemText = (code: string) => ({
  core_offline: '原 Core 当前离线',
  core_unreachable: '连接原 Core 超时或中断',
  core_timeout: '连接原 Core 超时',
  binding: '部署已变化，请核对原部署',
  invalid_account: '请核对原账号是否停用、阻塞或身份变化',
  invalid_path: 'Core 无法访问原录像',
  segment_file_unavailable: '原录像当前不可读',
  segment_changed: '录像或质量提示已变化，请核对',
  unsupported_option: 'Core 拒绝了当前发布范围或投稿参数',
  task_ambiguous: '找到多条匹配任务，需核对 Core',
  task_mismatch: '找到身份冲突的任务，需核对 Core',
  not_found: '原部署未找到对应记录',
  protocol: '无法核验 Core 回包，请核对原单',
}[code] || `请核对原部署和安排（${code}）`)
