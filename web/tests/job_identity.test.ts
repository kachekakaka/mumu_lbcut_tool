import { afterEach, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import JobDetail from '../src/components/JobDetail.vue'
import { api } from '../src/api'
import { tick } from '../src/poller'
import type { JobDetail as Job, Segment } from '../src/api/types'

enableAutoUnmount(afterEach)
vi.mock('../src/api', () => ({ api: { job: vi.fn(), segments: vi.fn() } }))

it('身份仅表示最近报告；已完成切片刷新后仍显示迟到的错房风险', async () => {
  const job = {
    id: 'job-k17', status: 'running', nickname: '测试主播', created_at: 1000,
    room_identity_status: 'unconfirmed', room_identity_confirmed_at: 1000,
    room_identity_age_sec: 31, room_identity_stale: true,
    room_identity_reason: 'runtime_identity_unavailable', recent_events: [],
  } as unknown as Job
  const segment = {
    job_id: job.id, segment_index: 1, status: 'finished', nickname: '测试主播',
    file_path: null, quality_flags: [],
  } as unknown as Segment
  vi.mocked(api.job).mockResolvedValue(job)
  vi.mocked(api.segments).mockResolvedValue({ items: [segment], total: 1, limit: 20, offset: 0, generated_at: 1031 })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div/>' } }] })
  const wrapper = mount(JobDetail, { props: { jobId: job.id }, global: { plugins: [ElementPlus, router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('最近报告')
  expect(wrapper.text()).toContain('确认依据已过期或尚未取得')
  expect(wrapper.text()).toContain('报告时证据年龄')
  expect(wrapper.text()).toContain('运行中身份核验过期，重新定位')

  vi.mocked(api.job).mockResolvedValue({ ...job, status: 'finished_with_warnings', room_identity_status: 'verified',
    room_identity_stale: false, room_identity_reason: null, room_identity_age_sec: 2 })
  vi.mocked(api.segments).mockResolvedValue({ items: [{ ...segment,
    quality_flags: [{ flag: 'identity_mismatch', suspected_start_time: 1000 }] }], total: 1, limit: 20, offset: 0, generated_at: 1040 })
  tick.value++
  await nextTick()
  await flushPromises()
  expect(wrapper.text()).toContain('最近报告')
  expect(wrapper.text()).toContain('完成但有警告')
  expect(wrapper.text()).toContain('可能包含错房画面')
})
