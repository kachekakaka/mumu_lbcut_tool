import { describe, expect, it } from 'vitest'
import { fmtDuration, fmtSize, fmtTime } from '../src/format'

describe('格式化', () => {
  it('同年显示 MM-DD HH:mm:ss，跨年带年份', () => {
    const sec = new Date(2026, 10, 11, 9, 5, 7).getTime() / 1000
    expect(fmtTime(sec, new Date(2026, 11, 31))).toBe('11-11 09:05:07')
    expect(fmtTime(sec, new Date(2027, 0, 1))).toBe('2026-11-11 09:05:07')
    expect(fmtTime(null)).toBe('—')
  })

  it('时长为 时:分:秒，缺失不伪造 0', () => {
    expect(fmtDuration(3725.9)).toBe('1:02:05')
    expect(fmtDuration(0)).toBe('0:00:00')
    expect(fmtDuration(null)).toBe('—')
    expect(fmtSize(812.44)).toBe('812.4 MB')
    expect(fmtSize(2048)).toBe('2.00 GB')
    expect(fmtSize(null)).toBe('—')
  })
})
