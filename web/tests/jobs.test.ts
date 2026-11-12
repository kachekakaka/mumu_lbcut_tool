import { describe, expect, it } from 'vitest'
import type { Job } from '../src/api/types'
import { endText } from '../src/jobs'

describe('恢复与最终停止分别表达', () => {
  it('登录中断、画面超时、取消恢复、下播停止同时可见', () => {
    const job = {
      recovery_context: {
        trigger: { code: 'login_prompt_detected', detail: null },
        last_failure: { code: 'render_timeout', detail: null }, outcome: 'cancelled',
      },
      stop_reason: { code: 'anchor_offline', detail: null },
      error_reason: { code: 'cancelled', detail: null }, exit_code: 30,
    } as Job
    expect(endText(job)).toContain('中断：出现登录提示')
    expect(endText(job)).toContain('恢复遇到：页面加载超时')
    expect(endText(job)).toContain('恢复结果：已取消')
    expect(endText(job)).toContain('停止：')
    expect(endText(job)).toContain('退出码 30 录制失败')
  })
  it('没有恢复事件时不制造中断原因', () => {
    expect(endText({ recovery_context: null, stop_reason: null, error_reason: null, exit_code: null } as Job)).toBe('—')
  })
})
