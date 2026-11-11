// 开发模拟数据：仅在 `npm run dev:mock`（或 VITE_MOCK=1）时加载，构建时禁止启用。
// 上传字段只用于检验上传界面的显示与隐藏，不代表后端契约。
import { ApiError, type RequestOptions, type Response } from '../api/client'
import type { Command, Instance, Job, Segment, Target } from '../api/types'

const now = () => Date.now() / 1000
const t0 = now()
let seq = 100
const nextId = (prefix: string) => `${prefix}${(++seq).toString(16).padStart(6, '0')}${Math.random().toString(16).slice(2, 10)}`

function target(id: string, raw: string, nickname: string | null, extra: Partial<Target> = {}): Target {
  return {
    id, raw, nickname, created_at: t0 - 86400, updated_at: t0 - 60, trusted_status: 'live', observed_status: 'live',
    observed_at: t0 - 30, confirmed_at: t0 - 30, auto_record: true, room_id: '7300000000', anchor_uid: '100000',
    session_key: `${raw}:7300000000`, recording_intent: false, suppressed: false, needs_fix_reason: null, note: null,
    attempts: 0, retry_at: 0, is_retrying: false, last_failure_reason: null, active_job_id: null, active_job_status: null, ...extra,
  }
}

const targets: (Target & { enabled: boolean })[] = [
  { ...target('t-alpha', 'https://live.douyin.com/111', '主播甲', { recording_intent: true, active_job_id: 'job-a1', active_job_status: 'running' }), enabled: true },
  { ...target('t-beta', 'https://live.douyin.com/222', '主播乙', { recording_intent: true, is_retrying: true, attempts: 2, retry_at: t0 + 120, last_failure_reason: { code: 'recovery_budget_exhausted', detail: null } }), enabled: true },
  { ...target('t-gamma', 'https://live.douyin.com/333', null, { trusted_status: 'ended', observed_status: 'ended', session_key: null, needs_fix_reason: { code: 'target_invalid', detail: '地址无效' } }), enabled: true },
  { ...target('t-delta', 'https://live.douyin.com/444', '主播丁', { suppressed: true, auto_record: false }), enabled: true },
  { ...target('t-epsilon', 'https://live.douyin.com/555', '主播戊', { trusted_status: 'mystery_state' }), enabled: true },
]

function job(id: string, targetId: string, status: string, extra: Partial<Job> = {}): Job {
  const t = targets.find((x) => x.id === targetId)
  return {
    id, target_id: targetId, nickname: t?.nickname ?? null, target_raw: t?.raw ?? null, vmindex: 0, status,
    created_at: t0 - 3600, started_at: t0 - 3590, recording_started_at: status === 'pending' ? null : t0 - 3550, ended_at: null, exit_code: null, stop_reason: null, error_reason: null,
    elapsed_sec: 3500, fps: 29.7, room_identity_status: 'verified', heartbeat_at: t0, finished_segment_count: 1,
    aborted_segment_count: 0, reconcile_requested: false, ...extra,
  }
}

const jobs: Job[] = [
  job('job-a1', 't-alpha', 'running'),
  job('job-b0', 't-beta', 'failed', { vmindex: 1, ended_at: t0 - 600, exit_code: 30, error_reason: { code: 'recovery_budget_exhausted', detail: null }, elapsed_sec: 1200, fps: 12.5 }),
  job('job-r9', 't-delta', 'needs_reconcile', { vmindex: 2, stop_reason: { code: 'old_worker_did_not_exit', detail: null }, error_reason: { code: 'old_worker_did_not_exit', detail: null }, room_identity_status: 'UNCONFIRMED' }),
  job('job-old', 't-alpha', 'finished_with_warnings', { created_at: t0 - 90000, started_at: t0 - 89990, ended_at: t0 - 80000, exit_code: 0, stop_reason: { code: 'anchor_offline', detail: null }, heartbeat_at: t0 - 80000 }),
  job('job-x', 't-epsilon', 'weird_status', { exit_code: 77 }),
]

const instances: Instance[] = [
  { vmindex: 0, name: 'MuMu-0', status: 'busy', job_id: 'job-a1', reason: null, updated_at: t0 - 3590, target_id: 't-alpha', nickname: '主播甲' },
  { vmindex: 1, name: 'MuMu-1', status: 'idle', job_id: null, reason: null, updated_at: t0 - 600, target_id: null, nickname: null },
  { vmindex: 2, name: 'MuMu-2', status: 'reconcile', job_id: 'job-r9', reason: null, updated_at: t0 - 300, target_id: 't-delta', nickname: '主播丁' },
  { vmindex: 3, name: 'MuMu-3', status: 'offline', job_id: null, reason: { code: 'android_not_started', detail: null }, updated_at: t0 - 50, target_id: null, nickname: null },
  { vmindex: 4, name: 'MuMu-4', status: 'offline', job_id: null, reason: { code: 'brand_new_reason', detail: null }, updated_at: t0 - 50, target_id: null, nickname: null },
]

function segment(jobId: string, index: number, status: string, extra: Partial<Segment> = {}): Segment {
  const j = jobs.find((x) => x.id === jobId)!
  return {
    job_id: jobId, target_id: j.target_id, nickname: j.nickname, target_raw: j.target_raw, segment_index: index, status,
    file_path: `D:\\mumu_test\\out\\${jobId}\\seg_${index}.mp4`, duration_sec: status === 'started' ? null : 1800,
    file_size_mb: status === 'started' ? null : 812.4, is_final: false, reason: null, created_at: t0 - 3500 + index * 1800,
    started_at: t0 - 3500 + index * 1800, ended_at: status === 'started' ? null : t0 - 1700 + index * 1800, updated_at: t0 - 60,
    quality_flags: [], ...extra,
  }
}

const segments: Segment[] = [
  segment('job-a1', 1, 'finished', { quality_flags: [{ flag: 'overlay_live_panel_window', suspected_start_time: t0 - 3000 }, { flag: 'low_frame_rate', suspected_start_time: t0 - 2500 }], upload: { status: 'succeeded', attempts: 1, last_error: null, updated_at: t0 - 1500 } }),
  segment('job-a1', 2, 'started', { upload: { status: 'pending', attempts: 0, last_error: null, updated_at: t0 - 1600 } }),
  segment('job-b0', 1, 'aborted', { reason: { code: 'recovery_budget_exhausted', detail: null }, quality_flags: [{ flag: 'brand_new_flag', suspected_start_time: null }], upload: { status: 'failed', attempts: 3, last_error: '网络超时', updated_at: t0 - 500 } }),
  segment('job-old', 1, 'finished', { is_final: true, quality_flags: [{ flag: 'static_picture', suspected_start_time: t0 - 85000 }], upload: { status: 'uploading', attempts: 1, last_error: null, updated_at: t0 - 100 } }),
]

const commands = new Map<string, Command>()
const byRequest = new Map<string, string>()

function page<T>(items: T[], q: Record<string, string>) {
  const limit = Number(q.limit ?? 50)
  const offset = Number(q.offset ?? 0)
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset, generated_at: now() }
}

const bool = (v: string | undefined) => (v === undefined ? undefined : v === 'true')

function queryOf(options: RequestOptions): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(options.query ?? {})) {
    if (v !== undefined && v !== null && v !== '') out[k] = String(v)
  }
  return out
}

function execute(command: Command, body: Record<string, unknown>) {
  const t = targets.find((x) => x.id === command.subject && x.enabled)
  const fail = (code: string, message: string) => Object.assign(command, { status: 'failed', error_code: code, message })
  const ok = (result: Record<string, unknown>, message = '已执行') => Object.assign(command, { status: 'succeeded', result, message })
  switch (command.kind) {
    case 'add': {
      const items = (body.targets as string[]).map((raw) => {
        if (raw.includes('bad')) return { input: raw, target_id: null, outcome: 'error', message: '地址为空或不支持', code: 'invalid_target' }
        const old = targets.find((x) => x.raw === raw)
        if (old) {
          const outcome = old.enabled ? 'existing' : 'restored'
          old.enabled = true
          return { input: raw, target_id: old.id, outcome, message: outcome, code: null }
        }
        const id = nextId('t-')
        targets.unshift({ ...target(id, raw, null, { auto_record: !body.manual_only, trusted_status: 'unknown' }), enabled: true })
        return { input: raw, target_id: id, outcome: 'created', message: '已新增监控', code: null }
      })
      return ok({ items })
    }
    case 'update':
      if (!t) return fail('target_not_found', '主播不存在或已移除')
      t.auto_record = body.auto_record as boolean
      return ok({ target_id: t.id, auto_record: t.auto_record })
    case 'remove':
      if (!t) return fail('target_not_found', '主播不存在或已移除')
      t.enabled = false
      return ok({ target_id: t.id, changed: true, affected_job_ids: t.active_job_id ? [t.active_job_id] : [] })
    case 'record': {
      if (!t) return fail('target_not_found', '主播不存在或已移除')
      if (t.active_job_status === 'stopping') return fail('job_stopping', '任务正在停止，请结束后重试')
      if (t.active_job_id) return ok({ target_id: t.id, job_id: t.active_job_id, outcome: 'existing' })
      if (t.trusted_status !== 'live') return fail('target_not_live', '主播未开播')
      const j = job(nextId('job-'), t.id, 'pending', { started_at: null, elapsed_sec: null, fps: null, heartbeat_at: null, room_identity_status: null, finished_segment_count: 0, created_at: now() })
      jobs.unshift(j)
      Object.assign(t, { active_job_id: j.id, active_job_status: 'pending', recording_intent: true, suppressed: false, needs_fix_reason: null })
      return ok({ target_id: t.id, job_id: j.id, outcome: 'queued' }, '已安排录制，实际状态请查看任务')
    }
    case 'stop_target': {
      if (!t) return fail('target_not_found', '主播不存在或已移除')
      if (t.session_key !== body.session_key) return fail('session_changed', '主播场次已经变化，请刷新后重试')
      const affected = t.active_job_id ? [t.active_job_id] : []
      const reconcile = jobs.filter((j) => j.target_id === t.id && j.status === 'needs_reconcile').map((j) => j.id)
      const outcome = t.suppressed ? 'already_stopped' : affected.length ? 'stop_requested' : 'intent_cancelled'
      Object.assign(t, { suppressed: true, recording_intent: false, is_retrying: false })
      for (const j of jobs) if (affected.includes(j.id)) j.status = 'stopping'
      if (t.active_job_id) t.active_job_status = 'stopping'
      return ok({ target_id: t.id, session_key: body.session_key, outcome, affected_job_ids: affected, reconcile_job_ids: reconcile })
    }
    case 'stop': {
      const j = jobs.find((x) => x.id === command.subject)
      if (!j) return fail('job_not_found', '任务不存在')
      if (j.status === 'needs_reconcile') return fail('reconcile_required', '任务需对账，不向未知进程发送信号')
      const outcome = !['pending', 'starting', 'running', 'stopping'].includes(j.status) ? 'already_terminal'
        : j.status === 'stopping' ? 'already_stopping' : j.status === 'pending' ? 'cancelled' : 'stop_requested'
      if (outcome === 'cancelled') j.status = 'cancelled'
      if (outcome === 'stop_requested') j.status = 'stopping'
      return ok({ job_id: j.id, outcome })
    }
    default:
      return fail('internal_error', '模拟数据不支持该命令')
  }
}

function submit(kind: string, subject: string, body: Record<string, unknown>, options: RequestOptions): Response<unknown> {
  const key = options.headers?.['Idempotency-Key']
  if (!key) throw new ApiError(422, 'invalid_request_id', '必须提供 UUID 格式的 Idempotency-Key')
  const existing = byRequest.get(key)
  if (existing) {
    const c = commands.get(existing)!
    if (c.kind !== kind || c.subject !== subject) throw new ApiError(409, 'idempotency_conflict', '同一请求编号不能用于不同操作')
    return { status: ['pending', 'running'].includes(c.status) ? 202 : 200, data: { command_id: c.id, status: c.status } }
  }
  const command: Command = {
    id: nextId('cmd-'), request_id: key, kind, subject, status: 'pending', result: null, error_code: null, message: null,
    created_at: now(), started_at: null, finished_at: null,
  }
  commands.set(command.id, command)
  byRequest.set(key, command.id)
  setTimeout(() => Object.assign(command, { status: 'running', started_at: now() }), 600)
  setTimeout(() => {
    // 地址里带 interrupt 时模拟服务重启导致命令中断
    if (JSON.stringify(body).includes('interrupt')) Object.assign(command, { status: 'interrupted', error_code: 'service_interrupted', message: '服务重启，命令已中断' })
    else execute(command, body)
    command.finished_at = now()
  }, 1500)
  return { status: 202, data: { command_id: command.id, status: 'pending' } }
}

function route(method: string, path: string, options: RequestOptions): Response<unknown> {
  const q = queryOf(options)
  const body = (options.body ?? {}) as Record<string, unknown>
  let m: RegExpMatchArray | null
  const enabled = targets.filter((t) => t.enabled)
  if (method === 'GET' && path === '/api/system') {
    return { status: 200, data: {
      scheduler_running: true, service_state: 'running', last_tick_at: now(), server_time: now(),
      upload_enabled: !location.search.includes('upload=0'), service_version: 'mock', api_version: 1,
      statistics_timezone: 'Asia/Shanghai', frontend_available: true,
    } }
  }
  if (method === 'GET' && path === '/api/overview') {
    const count = <T extends { status: string }>(list: T[]) => list.reduce<Record<string, number>>((a, x) => ({ ...a, [x.status]: (a[x.status] ?? 0) + 1 }), {})
    const alerts = [
      ...enabled.filter((t) => t.is_retrying).map((t) => ({ type: 'retrying', object_type: 'target', target_id: t.id, nickname: t.nickname, target_raw: t.raw, job_id: null, segment_index: null, reason: t.last_failure_reason, attempts: t.attempts, retry_at: t.retry_at, timestamp: t0 - 600 })),
      ...enabled.filter((t) => t.needs_fix_reason).map((t) => ({ type: 'needs_fix', object_type: 'target', target_id: t.id, nickname: t.nickname, target_raw: t.raw, job_id: null, segment_index: null, reason: t.needs_fix_reason, attempts: null, retry_at: null, timestamp: t0 - 700 })),
      ...jobs.filter((j) => j.status === 'needs_reconcile').map((j) => ({ type: 'needs_reconcile', object_type: 'job', target_id: j.target_id, nickname: j.nickname, target_raw: j.target_raw, job_id: j.id, segment_index: null, reason: j.error_reason, attempts: null, retry_at: null, timestamp: t0 - 300 })),
      ...segments.filter((s) => s.quality_flags.length).map((s) => ({ type: 'segment_quality', object_type: 'segment', target_id: s.target_id, nickname: s.nickname, target_raw: s.target_raw, job_id: s.job_id, segment_index: s.segment_index, reason: { code: 'segment_quality', detail: null }, attempts: null, retry_at: null, timestamp: s.updated_at })),
      ...segments.filter((s) => s.status === 'aborted').map((s) => ({ type: 'segment_aborted', object_type: 'segment', target_id: s.target_id, nickname: s.nickname, target_raw: s.target_raw, job_id: s.job_id, segment_index: s.segment_index, reason: s.reason, attempts: null, retry_at: null, timestamp: s.ended_at ?? s.updated_at })),
    ].sort((a, b) => b.timestamp - a.timestamp)
    const jc = count(jobs)
    return { status: 200, data: {
      instance_counts: { idle: 0, busy: 0, resetting: 0, offline: 0, reconcile: 0, ...count(instances) },
      job_counts: jc, recording_count: jc.running ?? 0, pending_count: jc.pending ?? 0,
      today_finished_segments: segments.filter((s) => s.status === 'finished').length, statistics_timezone: 'Asia/Shanghai',
      generated_at: now(), alerts: page(alerts, q),
    } }
  }
  if (method === 'GET' && path === '/api/instances') return { status: 200, data: page(instances.filter((i) => !q.status || i.status === q.status), q) }
  if (method === 'GET' && path === '/api/targets') {
    const list = enabled.filter((t) =>
      (!q.q || (t.nickname ?? '').includes(q.q) || t.raw.includes(q.q))
      && (!q.trusted_status || t.trusted_status === q.trusted_status)
      && (bool(q.auto_record) === undefined || t.auto_record === bool(q.auto_record))
      && (bool(q.has_notice) === undefined || (t.suppressed || !!t.needs_fix_reason || t.is_retrying) === bool(q.has_notice)))
    return { status: 200, data: page(list.map(({ enabled: _e, ...t }) => t), q) }
  }
  if (method === 'GET' && path === '/api/jobs') {
    const active = q.active !== 'false'
    const list = jobs.filter((j) => ['pending', 'starting', 'running', 'stopping', 'needs_reconcile'].includes(j.status) === active
      && (!q.target_id || j.target_id === q.target_id) && (!q.status || j.status === q.status)
      && (!q.from_time || j.created_at >= Number(q.from_time)) && (!q.to_time || j.created_at < Number(q.to_time)))
    return { status: 200, data: page(list, q) }
  }
  if (method === 'GET' && (m = path.match(/^\/api\/jobs\/([^/]+)\/log$/))) {
    const j = jobs.find((x) => x.id === decodeURIComponent(m![1]))
    if (!j) throw new ApiError(404, 'job_not_found', '任务不存在')
    if (j.status === 'pending') return { status: 200, data: { job_id: j.id, available: false, text: '', returned_lines: 0, truncated: false, reason: '任务尚未启动或日志尚未生成' } }
    const lines = Array.from({ length: 3000 }, (_, i) => `2026-11-11 10:00:${String(i % 60).padStart(2, '0')} 模拟日志第 ${i + 1} 行`)
    const n = Number(q.lines ?? 200)
    return { status: 200, data: { job_id: j.id, available: true, text: lines.slice(-n).join('\n'), returned_lines: n, truncated: true, reason: null } }
  }
  if (method === 'GET' && (m = path.match(/^\/api\/jobs\/([^/]+)$/))) {
    const j = jobs.find((x) => x.id === decodeURIComponent(m![1]))
    if (!j) throw new ApiError(404, 'job_not_found', '任务不存在')
    return { status: 200, data: { ...j, recent_events: [
      { event_id: 'e1', event: 'room_navigation', timestamp: t0 - 3580, received_at: t0 - 3580, action: 'enter', reason: { code: 'uid_matched', detail: null }, phase: 'verify', attempt: 1 },
      { event_id: 'e2', event: 'stall_detected', timestamp: t0 - 2000, received_at: t0 - 2000, action: null, reason: { code: 'video_frozen', detail: null }, phase: null, attempt: null },
      { event_id: 'e3', event: 'some_future_event', timestamp: null, received_at: t0 - 1000, action: null, reason: null, phase: null, attempt: null },
    ] } }
  }
  if (method === 'GET' && path === '/api/segments') {
    const list = segments.filter((s) => (!q.target_id || s.target_id === q.target_id) && (!q.job_id || s.job_id === q.job_id)
      && (!q.status || s.status === q.status)
      && (bool(q.has_quality_flags) === undefined || (s.quality_flags.length > 0) === bool(q.has_quality_flags))
      && (!q.upload_status || s.upload?.status === q.upload_status))
    return { status: 200, data: page(list, q) }
  }
  if (method === 'GET' && (m = path.match(/^\/api\/commands\/([^/]+)$/))) {
    const c = commands.get(decodeURIComponent(m[1]))
    if (!c) throw new ApiError(404, 'command_not_found', '命令不存在')
    return { status: 200, data: { ...c } }
  }
  if (method === 'POST' && path === '/api/targets') {
    if ((body.targets as string[]).some((x) => x.includes('netfail'))) throw new ApiError(0, 'network_error', '无法连接服务')
    return submit('add', '', body, options)
  }
  if ((m = path.match(/^\/api\/targets\/([^/]+)$/))) {
    const id = decodeURIComponent(m[1])
    if (method === 'DELETE') return submit('remove', id, {}, options)
    if (method === 'PATCH') return submit('update', id, body, options)
  }
  if (method === 'POST' && (m = path.match(/^\/api\/targets\/([^/]+)\/(record|stop)$/))) {
    return submit(m[2] === 'record' ? 'record' : 'stop_target', decodeURIComponent(m[1]), body, options)
  }
  if (method === 'POST' && (m = path.match(/^\/api\/jobs\/([^/]+)\/stop$/))) return submit('stop', decodeURIComponent(m[1]), {}, options)
  throw new ApiError(404, 'not_found', '接口或资源不存在')
}

export async function mockRequest<T>(method: string, path: string, options: RequestOptions): Promise<Response<T>> {
  await new Promise((r) => setTimeout(r, 120))
  return route(method, path, options) as Response<T>
}
