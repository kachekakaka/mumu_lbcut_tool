import type { Job } from './api/types'
import { exitCodeLabel, reasonLabel, stopReasonLabel } from './labels'

/** 心跳超过该秒数未更新时，提示帧率与时长不是实时值。 */
export const STALE_HEARTBEAT_SEC = 30

export function heartbeatAge(job: Job, serverTime: number | null | undefined): number | null {
  if (job.heartbeat_at === null || !serverTime) return null
  return Math.max(0, Math.round(serverTime - job.heartbeat_at))
}

export function endText(job: Job): string {
  const parts: string[] = []
  if (job.stop_reason) parts.push(stopReasonLabel(job.stop_reason))
  if (job.error_reason) parts.push(reasonLabel(job.error_reason))
  if (job.exit_code !== null) parts.push(`退出码 ${exitCodeLabel(job.exit_code)}`)
  return parts.length ? parts.join('；') : '—'
}

export const jobName = (job: { nickname: string | null; target_raw: string | null; target_id: string }) =>
  job.nickname || job.target_raw || job.target_id
