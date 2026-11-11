import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import SegmentTable from '../src/components/SegmentTable.vue'
import UploadStatus from '../src/components/UploadStatus.vue'
import type { Segment } from '../src/api/types'

const segment: Segment = {
  job_id: 'job-1', target_id: 't1', nickname: '主播甲', target_raw: 'raw', segment_index: 1, status: 'finished',
  file_path: 'D:/x.mp4', duration_sec: 60, file_size_mb: 10, is_final: false, reason: null, created_at: 0, started_at: 0,
  ended_at: 60, updated_at: 60, quality_flags: [{ flag: 'brand_new_flag', suspected_start_time: null }],
  upload: { status: 'failed', attempts: 3, last_error: '网络超时', updated_at: 60 },
}

async function render(uploadEnabled: boolean) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div/>' } }] })
  const wrapper = mount(SegmentTable, { props: { items: [segment], uploadEnabled }, global: { plugins: [ElementPlus, router] } })
  await nextTick()
  await nextTick()
  return wrapper
}

describe('上传显示', () => {
  it('上传未启用时不显示上传列', async () => {
    const wrapper = await render(false)
    expect(wrapper.text()).toContain('质量标记')
    expect(wrapper.text()).toContain('brand_new_flag')
    expect(wrapper.text()).not.toContain('上传状态')
    expect(wrapper.text()).not.toContain('上传失败')
  })

  it('上传启用时按状态类别显示', async () => {
    const wrapper = await render(true)
    expect(wrapper.text()).toContain('上传状态')
    expect(wrapper.text()).toContain('上传失败')
    const tag = mount(UploadStatus, { props: { upload: { status: 'succeeded', attempts: 1, last_error: null, updated_at: 0 } }, global: { plugins: [ElementPlus] } })
    expect(tag.find('.el-tag--success').exists()).toBe(true)
    const pending = mount(UploadStatus, { props: { upload: { status: 'uploading', attempts: 1, last_error: null, updated_at: 0 } }, global: { plugins: [ElementPlus] } })
    expect(pending.find('.el-tag--info').exists()).toBe(true)
    const failed = mount(UploadStatus, { props: { upload: { status: 'failed', attempts: 3, last_error: 'x', updated_at: 0 } }, global: { plugins: [ElementPlus] } })
    expect(failed.find('.el-tag--danger').exists()).toBe(true)
  })
})
