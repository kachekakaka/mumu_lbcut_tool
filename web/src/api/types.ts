// 字段与枚举以后端方案第 6 节为唯一契约；状态类字段保持 string，未知值由界面原样显示。

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  generated_at: number
}

export interface SystemInfo {
  scheduler_running: boolean
  service_state: string
  last_tick_at: number | null
  server_time: number
  upload_enabled: boolean
  service_version: string
  api_version: number
  statistics_timezone: string
  frontend_available: boolean
}

export interface Reason {
  code: string
  detail: string | null
}

export interface Alert {
  type: string
  object_type: string
  target_id: string | null
  job_id: string | null
  segment_index: number | null
  nickname: string | null
  target_raw: string | null
  reason: Reason | null
  attempts: number | null
  retry_at: number | null
  timestamp: number
}

export interface Overview {
  instance_counts: Record<string, number>
  job_counts: Record<string, number>
  recording_count: number
  pending_count: number
  today_finished_segments: number
  statistics_timezone: string
  generated_at: number
  alerts: Page<Alert>
}

export interface Instance {
  vmindex: number
  name: string
  status: string
  enabled: boolean
  job_id: string | null
  reason: Reason | null
  updated_at: number
  target_id: string | null
  nickname: string | null
}

export interface Target {
  id: string
  raw: string
  nickname: string | null
  created_at: number
  updated_at: number
  trusted_status: string
  observed_status: string
  observed_at: number | null
  confirmed_at: number | null
  auto_record: boolean
  room_id: string | null
  anchor_uid: string | null
  session_key: string | null
  recording_intent: boolean
  suppressed: boolean
  needs_fix_reason: Reason | null
  note: string | null
  attempts: number
  retry_at: number
  is_retrying: boolean
  last_failure_reason: Reason | null
  active_job_id: string | null
  active_job_status: string | null
}

export interface Job {
  id: string
  target_id: string
  nickname: string | null
  target_raw: string | null
  vmindex: number | null
  status: string
  created_at: number
  started_at: number | null
  recording_started_at: number | null
  ended_at: number | null
  exit_code: number | null
  stop_reason: Reason | null
  error_reason: Reason | null
  recovery_context: {
    trigger: Reason | null
    last_failure: Reason | null
    outcome: 'recovering' | 'recovered' | 'failed' | 'cancelled'
  } | null
  elapsed_sec: number | null
  fps: number | null
  room_identity_status: string | null
  heartbeat_at: number | null
  finished_segment_count: number
  aborted_segment_count: number
  reconcile_requested: boolean
}

export interface JobEvent {
  event_id: string | null
  event: string
  timestamp: number | null
  received_at: number
  action: string | null
  reason: Reason | null
  phase: string | null
  attempt: number | null
}

export interface JobDetail extends Job {
  recent_events: JobEvent[]
}

export interface QualityFlag {
  flag: string | null
  suspected_start_time: number | null
}

/** 仅开发模拟数据使用；真实字段由后续上传专项定义，不是接口契约。 */
export interface MockUploadState {
  status: string
  attempts: number
  last_error: string | null
  updated_at: number
}

export interface Segment {
  job_id: string
  target_id: string
  nickname: string | null
  target_raw: string | null
  segment_index: number
  status: string
  file_path: string | null
  duration_sec: number | null
  file_size_mb: number | null
  is_final: boolean
  reason: Reason | null
  created_at: number
  started_at: number | null
  ended_at: number | null
  updated_at: number
  quality_flags: QualityFlag[]
  upload?: MockUploadState
}

export interface JobLog {
  job_id: string
  available: boolean
  text: string
  returned_lines: number
  truncated: boolean
  reason: string | null
}

export interface Receipt {
  command_id: string
  status: string
}

export interface Command {
  id: string
  request_id: string
  kind: string
  subject: string
  status: string
  result: Record<string, any> | null
  error_code: string | null
  message: string | null
  created_at: number
  started_at: number | null
  finished_at: number | null
}
