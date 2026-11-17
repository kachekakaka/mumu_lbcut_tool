// 中文映射：未识别的值原样显示，空值显示“—”。
import type { Reason } from './api/types'

export type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

function pick(map: Record<string, string>, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const key = String(value)
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : key
}

function tag(map: Record<string, TagType>, value: unknown): TagType {
  const key = String(value ?? '')
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : 'info'
}

export const INSTANCE_STATUS: Record<string, string> = {
  idle: '空闲', busy: '录制中', booting: '开机中', offline: '离线', reconcile: '待对账',
}
const INSTANCE_TAG: Record<string, TagType> = {
  idle: 'success', busy: 'primary', booting: 'warning', offline: 'info', reconcile: 'danger',
}
export const INSTANCE_REASON: Record<string, string> = {
  android_not_started: '安卓系统未启动',
  booting: '正在自动开机',
  launch_failed: '自动开机失败（冷却重试中）',
  boot_interrupted: '开机被停服中断，等待重新探测',
  not_discovered: '未发现该实例',
}

export const JOB_STATUS: Record<string, string> = {
  pending: '排队中', starting: '启动中', running: '录制中', stopping: '停止中', needs_reconcile: '待对账',
  finished: '已完成', finished_with_warnings: '完成但有警告', failed: '失败', cancelled: '已取消',
}
const JOB_TAG: Record<string, TagType> = {
  pending: 'info', starting: 'warning', running: 'primary', stopping: 'warning', needs_reconcile: 'danger',
  finished: 'success', finished_with_warnings: 'warning', failed: 'danger', cancelled: 'info',
}
export const ACTIVE_JOB_STATUSES = ['pending', 'starting', 'running', 'stopping', 'needs_reconcile']
export const HISTORY_JOB_STATUSES = ['finished', 'finished_with_warnings', 'failed', 'cancelled']
export const STOPPABLE_JOB_STATUSES = ['pending', 'starting', 'running']

export const SEGMENT_STATUS: Record<string, string> = { started: '录制中', finished: '完成', aborted: '中止' }
const SEGMENT_TAG: Record<string, TagType> = { started: 'primary', finished: 'success', aborted: 'danger' }

export const TRUSTED_STATUS: Record<string, string> = { live: '在播', ended: '已下播', unknown: '未知' }
const TRUSTED_TAG: Record<string, TagType> = { live: 'success', ended: 'info', unknown: 'warning' }

export const EXIT_CODE: Record<string, string> = {
  '0': '正常结束', '2': '直播流结束', '9': '目标或运行条件需处理', '10': '确认未开播', '11': '开播检测服务异常',
  '15': '登录强阻断', '16': '预检卡顿', '17': '进房导航失败', '18': '错房或身份未确认', '20': '设备不可用',
  '30': '录制失败',
}

export const STOP_REASON: Record<string, string> = {
  manual_stop: '手动停止',
  target_removed: '主播已删除',
  anchor_offline: '主播下播',
  live_session_changed: '直播场次变化',
  scheduler_shutdown: '调度服务关闭',
  interrupted_host_restart: '主机重启中断',
  waiting_old_worker_exit: '等待旧 Worker 退出',
  old_worker_did_not_exit: '旧 Worker 未退出',
  unowned_process_requires_reconciliation: '未知归属进程需对账',
}

export const IDENTITY_STATUS: Record<string, string> = { verified: '已核验', mismatch: '错房', unconfirmed: '未确认' }
const IDENTITY_TAG: Record<string, TagType> = { verified: 'success', mismatch: 'danger', unconfirmed: 'warning' }

export const QUALITY_FLAG: Record<string, string> = {
  low_frame_rate: '低帧率',
  static_picture: '画面静止',
  visual_check_unavailable: '画面检查不可用',
  identity_unconfirmed: '身份未确认',
  identity_mismatch: '可能包含错房画面',
}
const OVERLAY_KIND: Record<string, string> = { live_panel_window: '直播面板' }

export const ALERT_TYPE: Record<string, string> = {
  retrying: '失败重试中', needs_fix: '需要修正', needs_reconcile: '需要对账',
  segment_quality: '切片质量标记', segment_aborted: '切片中止',
}
const ALERT_TAG: Record<string, TagType> = {
  retrying: 'warning', needs_fix: 'danger', needs_reconcile: 'danger', segment_quality: 'warning', segment_aborted: 'danger',
}

export const EVENT: Record<string, string> = {
  room_identity_check: '运行期身份核验',
  worker_started: 'Worker 启动', room_navigation: '进房导航', stall_detected: '发现卡顿', stall_healed: '卡顿恢复',
  overlay_recovery: '遮挡处理', login_blocked: '登录阻断', stream_ended: '直播流结束', worker_stopped: 'Worker 结束',
  worker_failed: 'Worker 失败', segment_quality_warning: '切片质量标记', login_status_checked: '登录后的开播状态复核',
}

export const COMMAND_STATUS: Record<string, string> = {
  submitting: '提交中', unknown: '结果不明', rejected: '未受理',
  pending: '已提交', running: '执行中', succeeded: '已执行', failed: '失败', interrupted: '已中断',
}
const COMMAND_TAG: Record<string, TagType> = {
  submitting: 'info', unknown: 'warning', rejected: 'danger', pending: 'info', running: 'primary',
  succeeded: 'success', failed: 'danger', interrupted: 'warning',
}

export const COMMAND_KIND: Record<string, string> = {
  add: '新增主播', update: '自动录制开关', remove: '删除主播', record: '立即开录',
  stop_target: '停止本场', stop: '平滑停止任务', reconcile: '对账', instance: '实例录播开关',
}

export const ADD_OUTCOME: Record<string, string> = { created: '已新增', existing: '已存在（设置不变）', restored: '已恢复监控', error: '失败' }
const ADD_TAG: Record<string, TagType> = { created: 'success', existing: 'info', restored: 'success', error: 'danger' }
export const RECORD_OUTCOME: Record<string, string> = { queued: '已安排录制', existing: '已有任务' }
export const STOP_TARGET_OUTCOME: Record<string, string> = {
  intent_cancelled: '已取消本场续录', stop_requested: '停止请求已生效', already_stopped: '本场已停止',
}
export const STOP_JOB_OUTCOME: Record<string, string> = {
  cancelled: '已取消排队任务', stop_requested: '停止请求已生效', already_stopping: '任务已在停止中', already_terminal: '任务已结束',
}

export const ERROR_CODE: Record<string, string> = {
  target_not_live: '主播未开播',
  live_status_unknown: '暂时无法确认开播状态',
  live_query_timeout: '开播查询超时',
  query_busy: '开播查询繁忙，请稍后重试',
  target_changed: '主播状态已变化，请刷新后重试',
  session_changed: '直播场次已变化，未停止新场次',
  job_stopping: '任务正在停止，结束后再操作',
  reconcile_required: '有遗留任务需要对账',
  service_interrupted: '服务重启，命令已中断',
  service_unavailable: '服务未就绪或正在关闭',
  idempotency_conflict: '请求编号冲突',
  target_not_found: '主播不存在或已删除',
  instance_not_found: '实例不存在',
  instance_busy: '实例正在占用中，请先停止对应录播任务后再停用',
  job_not_found: '任务不存在',
  command_not_found: '命令不存在',
  invalid_target: '地址无效或不支持',
  invalid_parameters: '请求参数错误',
  invalid_request_id: '请求编号无效',
  invalid_json: '请求格式错误',
  not_found: '接口或资源不存在',
  network_error: '无法连接服务',
  internal_error: '服务内部错误',
  log_read_failed: '日志读取失败',
}

export const instanceStatusLabel = (v: unknown) => pick(INSTANCE_STATUS, v)
export const instanceStatusType = (v: unknown) => tag(INSTANCE_TAG, v)
export const instanceReasonLabel = (v: Reason | null | undefined) => reasonLabel(v)
export const jobStatusLabel = (v: unknown) => pick(JOB_STATUS, v)
export const jobStatusType = (v: unknown) => tag(JOB_TAG, v)
export const segmentStatusLabel = (v: unknown) => pick(SEGMENT_STATUS, v)
export const segmentStatusType = (v: unknown) => tag(SEGMENT_TAG, v)
export const trustedStatusLabel = (v: unknown) => pick(TRUSTED_STATUS, v)
export const trustedStatusType = (v: unknown) => tag(TRUSTED_TAG, v)
export const stopReasonLabel = (v: Reason | null | undefined) => reasonLabel(v)
export const alertTypeLabel = (v: unknown) => pick(ALERT_TYPE, v)
export const alertTypeType = (v: unknown) => tag(ALERT_TAG, v)
export const eventLabel = (v: unknown) => pick(EVENT, v)
export const recoveryOutcomeLabel = (v: unknown) => pick({ recovering: '恢复中', recovered: '已恢复', failed: '恢复失败', cancelled: '已取消' }, v)
export const commandStatusLabel = (v: unknown) => pick(COMMAND_STATUS, v)
export const commandStatusType = (v: unknown) => tag(COMMAND_TAG, v)
export const commandKindLabel = (v: unknown) => pick(COMMAND_KIND, v)
export const addOutcomeLabel = (v: unknown) => pick(ADD_OUTCOME, v)
export const addOutcomeType = (v: unknown) => tag(ADD_TAG, v)
export const errorCodeLabel = (v: unknown) => pick(ERROR_CODE, v)

/** 后端以小写给出身份状态；大小写不同也按同一状态显示。 */
export function identityLabel(v: unknown): string {
  if (typeof v !== 'string' || !v) return pick(IDENTITY_STATUS, v)
  return IDENTITY_STATUS[v.toLowerCase()] ?? v
}
export const identityType = (v: unknown) => tag(IDENTITY_TAG, typeof v === 'string' ? v.toLowerCase() : v)

export function exitCodeLabel(code: number | null | undefined): string {
  if (code === null || code === undefined) return '—'
  const label = EXIT_CODE[String(code)]
  return label ? `${code} ${label}` : String(code)
}

export function qualityFlagLabel(flag: string | null | undefined): string {
  if (!flag) return '未知标记'
  if (flag.startsWith('overlay_')) {
    const kind = flag.slice('overlay_'.length)
    return `遮挡：${OVERLAY_KIND[kind] ?? kind}`
  }
  return pick(QUALITY_FLAG, flag)
}

/** 后端返回的稳定原因代码；详情独立显示，不解析内部字符串。 */
export const WORKER_REASON: Record<string, string> = {
  startup_identity_risk_boundary_reset: '准备期间身份曾未确认，重新准备采集',
  completed: '正常完成',
  recovery_budget_exhausted: '自动恢复次数用尽',
  video_frozen: '画面冻结',
  file_progress_stalled: '录制文件停止增长',
  file_write_stalled: '录制文件停止写入',
  process_exited: '录制进程退出',
  login_prompt_detected: '出现登录提示',
  anchor_live: '主播仍在播',
  anchor_ended_unconfirmed: '首次观测下播，等待确认',
  observation_superseded: '观测已过期，重新核对',
  login_blocked: '登录阻断',
  auth_required: '需要登录',
  render_timeout: '页面加载超时',
  navigation_failed: '进房导航失败',
  page_probe_unavailable: '页面检测不可用',
  overlay_not_recovered: '遮挡未能处理',
  cancelled: '已取消',
  intent_failed: '打开直播间失败',
  room_evidence_unavailable: '无法读取房间信息',
  identity_unstable: '房间身份尚未稳定',
  identity_conflict_unresolved: '房间身份冲突尚未解除',
  runtime_identity_mismatch: '运行中检测到错房，重新定位',
  runtime_identity_unavailable: '运行中身份核验过期，重新定位',
  runtime_identity_conflict_unresolved: '运行中身份冲突未解除',
  runtime_identity_probe_unavailable: '运行期身份核验不可用，需检查后手动重试',
  identity_relocating: '正在重新定位并确认身份',
  identity_risk_boundary_reset: '身份恢复后重开采集，封闭旧片风险范围',
  startup_identity_recheck: '开录前重新确认身份',
  startup_identity_changed: '开录准备期间身份变化',
  identity_probe_timeout: '身份读取超时',
  identity_probe_failed: '身份读取失败',
  target_identity_incomplete: '主播在播，身份标识未齐，等待重新查询',
  target_identity_stale: '身份信息已过期，等待重新查询',
  id_mismatch: '房间身份不符',
  identity_unconfirmed: '身份未确认',
  device_offline: '设备离线',
  capture_restart_failed: '采集重启失败',
  capture_or_muxer_exit_failed: '采集或封装进程异常退出',
  fragment_not_closed: '分片未正常关闭',
  no_recorded_segment: '未录到切片',
  startup_interrupted: '启动被中断',
  startup_gate_exhausted: '开录准备复核次数用尽',
  startup_left_live_page: '开录准备期间离开直播页',
  startup_overlay_detected: '开录准备期间出现遮挡',
  surface_probe_unavailable: '窗口状态检测不可用',
  worker_exception: 'Worker 异常',
  worker_failed: 'Worker 失败',
  event_collection_incomplete: '事件收集不完整',
  retrying: '录制失败，等待重试',
  needs_reconcile: '任务需要对账',
  segment_quality: '切片存在质量标记',
  segment_aborted: '切片中止',
  target_invalid: '目标无效',
  unspecified: '其他原因',
}

/** 未知代码保留，后端提供的详情按原文显示。 */
export function reasonLabel(value: Reason | null | undefined): string {
  if (!value) return '—'
  const label = pick({ ...INSTANCE_REASON, ...ERROR_CODE, ...WORKER_REASON, ...STOP_REASON }, value.code)
  return value.detail ? `${label}：${value.detail}` : label
}
