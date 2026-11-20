import { afterEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import SegmentsView from '../src/views/SegmentsView.vue'
import UploadStatus from '../src/components/UploadStatus.vue'
import type { Segment } from '../src/api/types'
import type { Arrangement } from '../src/api/segmentUploads'

const stubs = vi.hoisted(() => ({
  segments: vi.fn(), targets: vi.fn(), list: vi.fn(), show: vi.fn(),
  loader: null as null | (() => Promise<void>),
}))

vi.mock('../src/api', () => ({ api: { segments: stubs.segments, targets: stubs.targets } }))
vi.mock('../src/api/segmentUploads', async (original) => ({
  ...await original<typeof import('../src/api/segmentUploads')>(),
  onLocalConsole: () => window.location.origin === 'http://127.0.0.1:8000',
  segmentUploads: { list: stubs.list },
}))
vi.mock('../src/poller', async (original) => ({
  ...await original<typeof import('../src/poller')>(),
  usePageLoader: (loader: () => Promise<void>) => { stubs.loader = loader },
}))

enableAutoUnmount(afterEach)
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  stubs.list.mockReset()
  stubs.show.mockClear()
})

const segment: Segment = {
  job_id: 'job-1', target_id: 't1', nickname: '主播甲', target_raw: 'raw', segment_index: 1, status: 'finished',
  file_path: 'D:/x.mp4', duration_sec: 60, file_size_mb: 10, is_final: false, reason: null, created_at: 0, started_at: 0,
  ended_at: 60, updated_at: 60, quality_flags: [{ flag: 'brand_new_flag', suspected_start_time: null }],
  upload: { status: 'failed', attempts: 3, last_error: '网络超时', updated_at: 60 },
}

const conflicted: Segment = { ...segment, segment_index: 2, status: 'conflicted', file_path: null }
const saved: Arrangement = {
  id: 'old', job_id: conflicted.job_id, segment_index: conflicted.segment_index,
  work_id: 'work-old', task_id: 'platform-old', observed: 'SUCCEEDED', automatic: true,
  problem: null, rejected: false, can_revise: false, can_continue: false,
}

async function render(hostname: string) {
  vi.stubGlobal('location', { hostname, pathname: '/', origin: 'http://' + hostname + ':8000' })
  stubs.targets.mockResolvedValue({ items: [] })
  stubs.segments.mockResolvedValue({ items: [segment, conflicted], total: 2, offset: 0, limit: 50 })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div/>' } }] })
  await router.push('/')
  const wrapper = mount(SegmentsView, {
    global: {
      plugins: [ElementPlus, router],
      stubs: { SegmentUploadDialog: { template: '<div/>', setup(_, { expose }) { expose({ show: stubs.show }) } } },
    },
  })
  await stubs.loader?.()
  await flushPromises()
  return wrapper
}

describe('上传显示', () => {
  it('远端切片页隐藏本机投递详情，质量标记仍可读', async () => {
    const wrapper = await render('192.168.0.7')
    expect(wrapper.text()).toContain('质量标记')
    expect(wrapper.text()).toContain('brand_new_flag')
    expect(wrapper.text()).not.toContain('查看任务')
    expect(wrapper.text()).not.toContain('上传失败')
    expect(stubs.list).not.toHaveBeenCalled()
  })

  it('已封片可安排，冲突片只能查看原单，查询失败保留状态', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(10000)
    stubs.list.mockResolvedValueOnce({ items: [saved] }).mockRejectedValueOnce(new Error('断线'))
    const wrapper = await render('127.0.0.1')
    const rows = wrapper.findAll('.el-table__body tr')
    expect(rows).toHaveLength(2)
    const button = (row: number, label: string) => rows[row].findAll('button').find(btn => btn.text() === label)
    await button(0, '安排上传')!.trigger('click')
    expect(stubs.show).toHaveBeenLastCalledWith(segment)
    expect(rows[1].text()).toContain('终态冲突')
    expect(button(1, '安排上传') || button(1, '安排其他账号')).toBeUndefined()
    await button(1, '查看任务')!.trigger('click')
    expect(stubs.show).toHaveBeenLastCalledWith(conflicted, saved)
    now.mockReturnValue(16000)
    await stubs.loader?.()
    await flushPromises()
    expect(stubs.list).toHaveBeenCalledTimes(2)
    expect(rows[1].text()).toContain('平台已受理')
  })

  it('模拟上传标签按状态配色', () => {
    for (const [status, type] of [['succeeded', 'success'], ['uploading', 'info'], ['failed', 'danger']]) {
      const tag = mount(UploadStatus, { props: { upload: { status, attempts: 1, last_error: null, updated_at: 0 } }, global: { plugins: [ElementPlus] } })
      expect(tag.find(`.el-tag--${type}`).exists()).toBe(true)
    }
  })
})
