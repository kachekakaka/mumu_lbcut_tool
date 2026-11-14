import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import SegmentTable from '../src/components/SegmentTable.vue'
import { api } from '../src/api'
import type { Segment } from '../src/api/types'
import { connection } from '../src/poller'
import { ApiError } from '../src/api/client'

enableAutoUnmount(afterEach)

vi.mock('../src/api', () => ({
  api: {
    openSegmentFolder: vi.fn(),
  },
}))

vi.spyOn(ElMessage, 'success').mockImplementation(() => ({} as any))
vi.spyOn(ElMessage, 'error').mockImplementation(() => ({} as any))

function sampleSegment(extra: Partial<Segment> = {}): Segment {
  return {
    job_id: 'job-1',
    target_id: 'target-1',
    nickname: '主播A',
    target_raw: 'live.douyin.com/1',
    segment_index: 0,
    status: 'finished',
    file_path: 'D:/recordings/job-1/seg_000.mp4',
    duration_sec: 1800,
    file_size_mb: 250.5,
    is_final: false,
    reason: null,
    created_at: 1000,
    started_at: 1000,
    ended_at: 2800,
    updated_at: 2800,
    quality_flags: [],
    ...extra,
  }
}

async function renderTable(items: Segment[]) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div/>' } }] })
  const wrapper = mount(SegmentTable, {
    props: { items, uploadEnabled: false },
    global: { plugins: [ElementPlus, router] },
  })
  await nextTick()
  await nextTick()
  return wrapper
}

describe('SegmentTable 桌面定位交互', () => {
  beforeEach(() => {
    vi.mocked(api.openSegmentFolder).mockReset()
    vi.mocked(ElMessage.success).mockClear()
    vi.mocked(ElMessage.error).mockClear()
    connection.ok = true
    connection.system = null
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    connection.ok = null
    connection.system = null
  })

  it('本机发送成功后提示去桌面确认，不宣称已完成定位', async () => {
    vi.stubGlobal('location', { hostname: 'localhost' })
    vi.mocked(api.openSegmentFolder).mockResolvedValueOnce({
      status: 'succeeded',
      file_path: 'D:/recordings/job-1/seg_000.mp4',
      folder_path: 'D:/recordings/job-1',
      revealed: true,
    })

    const wrapper = await renderTable([sampleSegment()])
    const buttons = wrapper.findAll('button')
    const copyBtn = buttons.find((b) => b.text().includes('复制'))
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(copyBtn).toBeDefined()
    expect(openBtn).toBeDefined()

    await openBtn!.trigger('click')
    expect(api.openSegmentFolder).toHaveBeenCalledWith('job-1', 0)
    expect(ElMessage.success).toHaveBeenCalledWith('已发送打开目录请求，请在本机桌面确认')
  })

  it('远程发送成功后明确提示去录制机桌面确认', async () => {
    vi.stubGlobal('location', { hostname: '192.168.3.250' })
    vi.mocked(api.openSegmentFolder).mockResolvedValueOnce({
      status: 'succeeded',
      file_path: 'D:/recordings/job-1/seg_000.mp4',
      folder_path: 'D:/recordings/job-1',
      revealed: true,
    })

    const wrapper = await renderTable([sampleSegment()])
    const buttons = wrapper.findAll('button')
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(openBtn).toBeDefined()

    await openBtn!.trigger('click')
    expect(api.openSegmentFolder).toHaveBeenCalledWith('job-1', 0)
    expect(ElMessage.success).toHaveBeenCalledWith('已向录制机发送打开目录请求，请在录制机桌面确认')
  })

  it('打开失败时弹出错误提示', async () => {
    vi.stubGlobal('location', { hostname: 'localhost' })
    vi.mocked(api.openSegmentFolder).mockRejectedValueOnce(new ApiError(500, 'desktop_open_failed', '无法启动录制机文件管理器，请查看服务日志'))

    const wrapper = await renderTable([sampleSegment()])
    const buttons = wrapper.findAll('button')
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(openBtn).toBeDefined()

    await openBtn!.trigger('click')
    expect(ElMessage.error).toHaveBeenCalledWith('无法启动录制机文件管理器，请查看服务日志')
    expect(ElMessage.success).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1500)
    expect(openBtn!.attributes('disabled')).toBeUndefined()
  })

  it('file_path 为空时不展示打开目录按钮', async () => {
    const wrapper = await renderTable([sampleSegment({ file_path: null })])
    const buttons = wrapper.findAll('button')
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(openBtn).toBeUndefined()
  })

  it.each([null, false])('连接状态为 %s 时禁止打开，恢复后可操作且不依赖调度器', async (state) => {
    connection.ok = state
    const wrapper = await renderTable([sampleSegment()])
    const buttons = wrapper.findAllComponents({ name: 'ElButton' })
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))!
    const copyBtn = buttons.find((b) => b.text().includes('复制'))!
    expect(openBtn.props('disabled')).toBe(true)
    expect(copyBtn.find('button').attributes('disabled')).toBeUndefined()
    // 绕过按钮原生 disabled，核验函数入口同样拒绝断线请求。
    openBtn.vm.$emit('click', new MouseEvent('click'))
    await flushPromises()
    expect(api.openSegmentFolder).not.toHaveBeenCalled()

    connection.ok = true
    await nextTick()
    expect(openBtn.props('disabled')).toBe(false)
    vi.mocked(api.openSegmentFolder).mockResolvedValueOnce({ status: 'succeeded', file_path: 'f', folder_path: 'd', revealed: true })
    await openBtn.trigger('click')
    await flushPromises()
    expect(api.openSegmentFolder).toHaveBeenCalledTimes(1)
  })

  it('请求未完成及完成后 1.5 秒内，连点同一行或其他行不会重复请求', async () => {
    let finish!: () => void
    vi.mocked(api.openSegmentFolder).mockImplementationOnce(() => new Promise((resolve) => {
      finish = () => resolve({ status: 'succeeded', file_path: 'f', folder_path: 'd', revealed: true })
    }))
    const wrapper = await renderTable([sampleSegment(), sampleSegment({ segment_index: 1 })])
    const buttons = wrapper.findAllComponents({ name: 'ElButton' }).filter((b) => b.text().includes('打开目录'))
    await buttons[0]!.trigger('click')
    await nextTick()
    expect(buttons.every((b) => b.props('disabled'))).toBe(true)
    buttons[0]!.vm.$emit('click', new MouseEvent('click'))
    buttons[1]!.vm.$emit('click', new MouseEvent('click'))
    await vi.advanceTimersByTimeAsync(2000)
    expect(api.openSegmentFolder).toHaveBeenCalledTimes(1)
    expect(buttons[0]!.props('loading')).toBe(true)
    finish()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1499)
    expect(buttons[1]!.props('disabled')).toBe(true)
    buttons[1]!.vm.$emit('click', new MouseEvent('click'))
    expect(api.openSegmentFolder).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(buttons.every((b) => !b.props('disabled'))).toBe(true)
    vi.mocked(api.openSegmentFolder).mockResolvedValueOnce({ status: 'succeeded', file_path: 'f', folder_path: 'd', revealed: false })
    await buttons[1]!.trigger('click')
    await flushPromises()
    expect(api.openSegmentFolder).toHaveBeenCalledTimes(2)
  })
})
