import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { api } from '../src/api'
import type { Instance } from '../src/api/types'
import {
  disableInstanceReason, disabledInstanceCount, hostPowerHint, visibleOverviewInstances,
} from '../src/instances'
import OverviewView from '../src/views/OverviewView.vue'
import InstancesView from '../src/views/InstancesView.vue'

afterEach(() => vi.restoreAllMocks())

function inst(extra: Partial<Instance> = {}): Instance {
  return {
    vmindex: 2, name: 'lb1', status: 'idle', enabled: true, job_id: null,
    reason: null, updated_at: 1, target_id: null, nickname: null, ...extra,
  }
}

describe('实例界面规则', () => {
  it('占用、开机、待对账时拒绝直接停用并给出原因', () => {
    expect(disableInstanceReason(inst({ enabled: true, status: 'idle', job_id: null }))).toBe('')
    expect(disableInstanceReason(inst({ enabled: false, status: 'busy', job_id: 'j1' }))).toBe('')
    expect(disableInstanceReason(inst({ enabled: true, status: 'busy', job_id: 'j1' }))).toContain('停止对应任务')
    expect(disableInstanceReason(inst({ enabled: true, status: 'booting', job_id: null }))).toContain('开机')
    expect(disableInstanceReason(inst({ enabled: true, status: 'reconcile', job_id: 'j2' }))).toContain('对账')
  })

  it('未启用实例保留本机开机提示，总览只统计和展示已启用及待对账实例', () => {
    expect(hostPowerHint(inst({ enabled: false, status: 'idle' }))).toBe('本机已开机')
    expect(hostPowerHint(inst({ enabled: false, status: 'offline', reason: { code: 'android_not_started', detail: null } }))).toBe('本机未开机')

    const list = [
      inst({ vmindex: 2, enabled: true, status: 'idle' }),
      inst({ vmindex: 3, enabled: false, status: 'idle' }),
      inst({ vmindex: 4, enabled: false, status: 'reconcile', job_id: 'j4' }),
    ]
    expect(visibleOverviewInstances(list).map((x) => x.vmindex)).toEqual([2, 4])
    expect(disabledInstanceCount(list)).toBe(1)
  })

  it('总览显示开机中实例计数，实例页支持 booting 筛选与展示开机中标签', async () => {
    const bootingRow = inst({ vmindex: 5, name: 'MuMu-5', status: 'booting', enabled: true, reason: { code: 'booting', detail: null } })
    const failedRow = inst({ vmindex: 6, name: 'MuMu-6', status: 'offline', enabled: true, reason: { code: 'launch_failed', detail: null } })
    vi.spyOn(api, 'overview').mockResolvedValue({
      instance_counts: { idle: 0, busy: 0, booting: 1, offline: 1, reconcile: 0 },
      job_counts: {},
      recording_count: 0,
      pending_count: 1,
      today_finished_segments: 0,
      statistics_timezone: 'Asia/Shanghai',
      generated_at: 100,
      alerts: { items: [], total: 0, limit: 20, offset: 0, generated_at: 100 },
    })
    const instancesSpy = vi.spyOn(api, 'instances').mockResolvedValue({
      items: [bootingRow, failedRow], total: 2, limit: 50, offset: 0, generated_at: 100,
    })

    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div/>' } }] })
    const overview = mount(OverviewView, { global: { plugins: [ElementPlus, router] } })
    await nextTick()
    await new Promise((r) => setTimeout(r, 0))
    await nextTick()
    expect(overview.text()).toContain('开机中 1')
    overview.unmount()

    const view = mount(InstancesView, { global: { plugins: [ElementPlus, router] } })
    await nextTick()
    await new Promise((r) => setTimeout(r, 0))
    await nextTick()
    expect(view.text()).toContain('开机中')
    expect(view.text()).toContain('正在自动开机')
    expect(view.text()).toContain('自动开机失败（冷却重试中）')
    ;(view.vm as unknown as { filters: { status: string } }).filters.status = 'booting'
    await nextTick()
    await new Promise((r) => setTimeout(r, 0))
    expect(instancesSpy).toHaveBeenCalledWith(expect.objectContaining({ status: 'booting' }))
    view.unmount()
  })
})
