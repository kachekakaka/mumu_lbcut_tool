import { describe, expect, it } from 'vitest'
import {
  exitCodeLabel, identityLabel, instanceReasonLabel, instanceStatusLabel, instanceStatusType, jobStatusLabel, qualityFlagLabel,
  reasonLabel, segmentStatusLabel, stopReasonLabel, trustedStatusLabel,
} from '../src/labels'
const r = (code: string, detail: string | null = null) => ({ code, detail })

describe('中文映射', () => {
  it('已知状态与原因显示中文', () => {
    expect(instanceStatusLabel('idle')).toBe('空闲')
    expect(instanceStatusLabel('reconcile')).toBe('待对账')
    expect(instanceReasonLabel(r('android_not_started'))).toBe('安卓系统未启动')
    expect(instanceReasonLabel(r('not_discovered'))).toBe('未发现该实例')
    expect(jobStatusLabel('finished_with_warnings')).toBe('完成但有警告')
    expect(jobStatusLabel('needs_reconcile')).toBe('待对账')
    expect(segmentStatusLabel('aborted')).toBe('中止')
    expect(trustedStatusLabel('ended')).toBe('已下播')
    expect(stopReasonLabel(r('unowned_process_requires_reconciliation'))).toBe('未知归属进程需对账')
    expect(exitCodeLabel(0)).toBe('0 正常结束')
    expect(exitCodeLabel(15)).toBe('15 登录强阻断')
    expect(exitCodeLabel(30)).toBe('30 录制失败')
    expect(reasonLabel(r('recovery_budget_exhausted'))).toBe('自动恢复次数用尽')
    expect(reasonLabel(r('video_frozen'))).toBe('画面冻结')
    expect(reasonLabel(r('old_worker_did_not_exit'))).toBe('旧 Worker 未退出')
    expect(reasonLabel(r('capture_restart_failed', 'adb timeout'))).toBe('采集重启失败：adb timeout')
    expect(reasonLabel(r('worker_exception', 'boom: detail'))).toBe('Worker 异常：boom: detail')
  })

  it('未知值原样显示，空值显示破折号', () => {
    expect(instanceStatusLabel('brand_new')).toBe('brand_new')
    expect(instanceStatusType('brand_new')).toBe('info')
    expect(instanceReasonLabel(r('brand_new_reason'))).toBe('brand_new_reason')
    expect(jobStatusLabel('weird_status')).toBe('weird_status')
    expect(trustedStatusLabel('mystery_state')).toBe('mystery_state')
    expect(exitCodeLabel(77)).toBe('77')
    expect(exitCodeLabel(null)).toBe('—')
    expect(jobStatusLabel(null)).toBe('—')
    expect(reasonLabel(r('unspecified', '录制进程崩溃'))).toBe('其他原因：录制进程崩溃')
    expect(reasonLabel(r('brand_new', 'detail'))).toBe('brand_new：detail')
    expect(reasonLabel(r('toString'))).toBe('toString')
    expect(jobStatusLabel('toString')).toBe('toString')
  })

  it('质量标记：遮挡前缀、已知标记与未知标记', () => {
    expect(qualityFlagLabel('overlay_live_panel_window')).toBe('遮挡：直播面板')
    expect(qualityFlagLabel('overlay_new_kind')).toBe('遮挡：new_kind')
    expect(qualityFlagLabel('low_frame_rate')).toBe('低帧率')
    expect(qualityFlagLabel('static_picture')).toBe('画面静止')
    expect(qualityFlagLabel('visual_check_unavailable')).toBe('画面检查不可用')
    expect(qualityFlagLabel('identity_unconfirmed')).toBe('身份未确认')
    expect(qualityFlagLabel('brand_new_flag')).toBe('brand_new_flag')
    expect(qualityFlagLabel(null)).toBe('未知标记')
  })

  it('身份状态不区分大小写，未知值原样', () => {
    expect(identityLabel('verified')).toBe('已核验')
    expect(identityLabel('UNCONFIRMED')).toBe('未确认')
    expect(identityLabel('something')).toBe('something')
    expect(identityLabel(null)).toBe('—')
  })
})
