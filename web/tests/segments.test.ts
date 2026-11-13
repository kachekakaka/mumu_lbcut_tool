import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import SegmentTable from '../src/components/SegmentTable.vue'
import { api } from '../src/api'
import type { Segment } from '../src/api/types'

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
    vi.clearAllMocks()
  })

  it('文件存在时展示打开目录按钮，并在本机访问成功时提示“已打开所在目录”', async () => {
    vi.stubGlobal('location', { hostname: 'localhost' })
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
    expect(ElMessage.success).toHaveBeenCalledWith('已打开所在目录')
  })

  it('在远程/第二设备访问成功时提示“已在录制机桌面打开所在目录”', async () => {
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
    expect(ElMessage.success).toHaveBeenCalledWith('已在录制机桌面打开所在目录')
  })

  it('打开失败时弹出错误提示', async () => {
    vi.stubGlobal('location', { hostname: 'localhost' })
    vi.mocked(api.openSegmentFolder).mockRejectedValueOnce(new Error('切片所在目录不存在'))

    const wrapper = await renderTable([sampleSegment()])
    const buttons = wrapper.findAll('button')
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(openBtn).toBeDefined()

    await openBtn!.trigger('click')
    expect(ElMessage.error).toHaveBeenCalledWith('切片所在目录不存在')
  })

  it('file_path 为空时不展示打开目录按钮', async () => {
    const wrapper = await renderTable([sampleSegment({ file_path: null })])
    const buttons = wrapper.findAll('button')
    const openBtn = buttons.find((b) => b.text().includes('打开目录'))
    expect(openBtn).toBeUndefined()
  })
})
