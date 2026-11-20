import { afterEach, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import SessionPostingDialog from '../src/components/SessionPostingDialog.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/bilibili', () => ({ request }))
enableAutoUnmount(afterEach)
afterEach(() => request.mockReset())

it('原部署离线时仍可关闭自动投稿，并显示既有任务和 BV 号', async () => {
  const route = '/session-uploads/主播'
  request.mockImplementation(async (path: string, body?: { enabled: boolean }) => {
    if (body) return { enabled: body.enabled }
    if (path.endsWith('/settings')) return {
      enabled: true, saved: { deployment: 'original', category_id: 171,
        account: { id: 'a', display_name: '测试账号', remote_user_id: '42' } },
      deployment: null, accounts: [], problem: '原 Core 当前离线',
    }
    return { items: [{ session_key: 'room:1', started_at: 1, title: '开录标题', status: 'delivered',
      platform_status: 'SUCCEEDED', task_id: 'original-task', bvid: 'BV123', problem: null }] }
  })
  const wrapper = mount(SessionPostingDialog, {
    attachTo: document.body,
    props: { modelValue: true, targetId: '主播', nickname: '测试主播' },
    global: { plugins: [ElementPlus], stubs: { teleport: true } },
  })
  await flushPromises()
  expect(wrapper.text()).toContain('开录标题')
  expect(wrapper.text()).toContain('已交付 · 投稿成功')
  expect(wrapper.text()).toContain('original-task')
  expect(wrapper.text()).toContain('BV123')
  expect(wrapper.text()).toContain('原 Core 当前离线')
  expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
  await wrapper.get('.el-switch__input').setValue(false)
  expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('form').trigger('submit')
  expect(request).toHaveBeenCalledWith(route + '/settings', { enabled: false }, expect.any(String))
})
