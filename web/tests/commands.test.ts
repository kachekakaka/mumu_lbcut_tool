import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { ApiError } from '../src/api/client'
import type { Command } from '../src/api/types'
import { createOperations, describeOperation, type OperationDeps } from '../src/commands'
import { ops as drawerOperations } from '../src/operations'
import OperationsDrawer from '../src/components/OperationsDrawer.vue'

const drawerRequest = vi.hoisted(() => vi.fn<(id: string, signal: AbortSignal) => Promise<Command>>())
vi.mock('../src/operations', async () => {
  const { createOperations } = await import('../src/commands')
  return { ops: createOperations({ getCommand: drawerRequest, newId: () => 'drawer-test', refresh: () => {} }) }
})

function command(status: string, extra: Partial<Command> = {}): Command {
  return {
    id: 'cmd-1', request_id: 'req', kind: 'record', subject: 't1', status, result: null, error_code: null, message: null,
    created_at: 0, started_at: null, finished_at: null, ...extra,
  }
}

function setup(responses: (Command | Error)[]) {
  const states: string[] = []
  let ids = 0
  const deps: OperationDeps = {
    getCommand: vi.fn(async () => {
      const next = responses.shift()
      if (!next) throw new Error('no more responses')
      if (next instanceof Error) throw next
      return next
    }),
    newId: () => `id-${++ids}`,
    refresh: vi.fn(),
    sleep: async () => undefined,
    notify: vi.fn((op) => states.push(op.state)),
  }
  return { ops: createOperations(deps), deps, states }
}

const settle = () => new Promise((r) => setTimeout(r, 0))

describe('命令追踪', () => {
  it('受理后跟踪到终态，再刷新数据', async () => {
    const { ops, deps } = setup([
      command('pending'),
      command('running'),
      command('succeeded', { result: { target_id: 't1', job_id: 'j1', outcome: 'queued' } }),
    ])
    const seen: string[] = []
    const op = ops.start('record', 't1', '立即开录', async () => ({ command_id: 'cmd-1', status: 'pending' }), (o) => seen.push(o.state))
    expect(op.state).toBe('submitting')
    expect(ops.busy('t1', 'record')).toBe(true)
    await settle()
    expect(op.state).toBe('succeeded')
    expect(deps.getCommand).toHaveBeenCalledTimes(3)
    expect(deps.refresh).toHaveBeenCalledTimes(1)
    expect(seen).toEqual(['succeeded'])
    expect(ops.busy('t1', 'record')).toBe(false)
    expect(describeOperation(op)).toMatchObject({ level: 'success', text: '已安排录制' })
  })

  it('网络结果不明时保留请求编号，重试使用同一编号', async () => {
    const done = command('succeeded', { result: { job_id: 'j1', outcome: 'stop_requested' }, kind: 'stop' })
    const { ops } = setup([done, done])
    const keys: string[] = []
    let fail = true
    const op = ops.start('stop', 'j1', '平滑停止', async (key) => {
      keys.push(key)
      if (fail) throw new ApiError(0, 'network_error', '无法连接服务')
      return { command_id: 'cmd-1', status: 'succeeded' }
    })
    await settle()
    expect(op.state).toBe('unknown')
    expect(describeOperation(op).notes.join()).toContain('同一请求编号')
    fail = false
    expect(ops.retry(op)).toBe(true)
    await settle()
    expect(op.state).toBe('succeeded')
    expect(keys).toEqual([op.requestId, op.requestId])
    // 新操作生成新编号
    const other = ops.start('stop', 'j2', '平滑停止', async () => ({ command_id: 'cmd-1', status: 'succeeded' }))
    expect(other.requestId).not.toBe(op.requestId)
  })

  it('HTTP 层拒绝不允许重试，显示错误码中文', async () => {
    const { ops } = setup([])
    const op = ops.start('stop_target', 't1', '停止本场', async () => {
      throw new ApiError(409, 'session_changed', '主播场次已经变化，请刷新后重试')
    })
    await settle()
    expect(op.state).toBe('rejected')
    expect(ops.retry(op)).toBe(false)
    expect(describeOperation(op).text).toBe('未受理：直播场次已变化，未停止新场次')
  })

  it('服务关闭时的 503 不显示为排队成功', async () => {
    const { ops } = setup([])
    const op = ops.start('record', 't1', '立即开录', async () => {
      throw new ApiError(503, 'service_unavailable', '服务未就绪或正在关闭')
    })
    await settle()
    expect(op.state).toBe('rejected')
    expect(describeOperation(op)).toMatchObject({ level: 'error', text: '未受理：服务未就绪或正在关闭' })
  })

  it('命令失败与中断分别说明', async () => {
    const { ops } = setup([
      command('failed', { error_code: 'target_not_live', message: '主播未开播' }),
      command('interrupted', { error_code: 'service_interrupted' }),
    ])
    const failed = ops.start('record', 't1', '立即开录', async () => ({ command_id: 'cmd-1', status: 'failed' }))
    await settle()
    expect(describeOperation(failed)).toMatchObject({ level: 'error', text: '失败：主播未开播' })
    const interrupted = ops.start('record', 't1', '立即开录', async () => ({ command_id: 'cmd-2', status: 'interrupted' }))
    await settle()
    expect(interrupted.state).toBe('interrupted')
    expect(describeOperation(interrupted).notes.join()).toContain('重新操作')
  })

  it('查询命令时断线继续跟踪，恢复后到达终态', async () => {
    const { ops } = setup([new ApiError(0, 'network_error', ''), command('succeeded', { kind: 'remove', result: {} })])
    const op = ops.start('remove', 't1', '删除主播', async () => ({ command_id: 'cmd-1', status: 'pending' }))
    await settle()
    expect(op.state).toBe('succeeded')
    expect(op.followError).toBe(false)
  })

  it('31 个在途命令保留原记录，完成后只裁剪已结束历史', async () => {
    const queries = new Map<string, (result: Command) => void>()
    const callbacks = vi.fn()
    drawerRequest.mockImplementation((id) => new Promise((resolve) => queries.set(id, resolve)))
    const view = mount(OperationsDrawer, {
      props: { modelValue: true },
      global: { plugins: [ElementPlus], stubs: { ElDrawer: { template: '<section><slot/></section>' } } },
    })
    try {
      for (let i = 0; i < 31; i++) {
        drawerOperations.start('record', String(i), `开录 ${i}`,
          async () => ({ command_id: String(i), status: 'pending' }), callbacks)
      }
      await flushPromises()
      expect(drawerOperations.list).toHaveLength(31)
      expect(view.findAll('.op-card')).toHaveLength(31)
      expect(view.findAll('.op-card').at(-1)?.text()).toContain('开录 0')
      expect(drawerOperations.busy('0')).toBe(true)
      expect(queries.size).toBe(31)
      queries.forEach((resolve) => resolve(command('succeeded')))
      await flushPromises()
      expect(callbacks).toHaveBeenCalledTimes(31)
      expect(view.findAll('.op-card')).toHaveLength(30)
      expect(drawerOperations.busy('0')).toBe(false)
    } finally {
      drawerOperations.dispose()
      view.unmount()
    }
  })

  it('结束跟踪中断查询，忽略迟到回包并释放已销毁页面的回调', async () => {
    const call = vi.fn()
    let resolve!: (result: Command) => void
    let signal!: AbortSignal
    let rejectFirst = true
    const owner = Symbol()
    const ops = createOperations({
      getCommand: vi.fn(async (_id: string, abort: AbortSignal) => {
        signal = abort
        if (rejectFirst) {
          rejectFirst = false
          throw new ApiError(0, 'network_error', '')
        }
        return new Promise<Command>((done) => { resolve = done })
      }),
      newId: () => 'original-key',
      refresh: vi.fn(),
      sleep: async () => undefined,
    })
    const send = vi.fn(async () => ({ command_id: 'cmd-1', status: 'pending' }))
    const op = ops.start('record', 'a', '开录', send, call, owner)
    await settle()
    expect(op.followError).toBe(true)
    ops.detachOwner(owner)
    ops.endTracking(op)
    expect(signal.aborted).toBe(true)
    resolve(command('succeeded'))
    await settle()
    expect(op.state).toBe('pending')
    expect(send).toHaveBeenCalledTimes(1)
    expect(call).not.toHaveBeenCalled()
    expect(ops.list).toHaveLength(0)

    const unknown = ops.start('record', 'b', '开录', async () => { throw new ApiError(0, 'network_error', '') }, call)
    await settle()
    expect(unknown.state).toBe('unknown')
    ops.endTracking(unknown)
    expect(ops.retry(unknown)).toBe(false)
    expect(ops.list).toHaveLength(0)
  })

  it('批量新增部分失败不显示为全部成功', () => {
    const { ops } = setup([])
    const op = ops.list[0] ?? ops.start('add', '', '新增', () => new Promise(() => undefined))
    op.state = 'succeeded'
    op.command = command('succeeded', {
      kind: 'add',
      result: { items: [
        { input: 'a', outcome: 'created' }, { input: 'b', outcome: 'existing' }, { input: 'c', outcome: 'restored' },
        { input: 'd', outcome: 'error', code: 'invalid_target' },
      ] },
    })
    expect(describeOperation(op)).toMatchObject({ level: 'warning', text: '部分失败：新增 1，已存在 1，已恢复 1，失败 1' })
  })

  it('停止本场遇到待对账任务时说明遗留进程仍需对账', () => {
    const { ops } = setup([])
    const op = ops.start('stop_target', 't1', '停止本场', () => new Promise(() => undefined))
    op.state = 'succeeded'
    op.command = command('succeeded', {
      kind: 'stop_target',
      result: { outcome: 'intent_cancelled', affected_job_ids: [], reconcile_job_ids: ['j9'] },
    })
    const d = describeOperation(op)
    expect(d.level).toBe('warning')
    expect(d.text).toBe('已取消本场续录')
    expect(d.notes.join()).toContain('遗留进程仍需对账')
  })

  it('实例启停命令与占用拒绝分别给出明确说明', async () => {
    const { ops } = setup([
      command('succeeded', { kind: 'instance', result: { vmindex: 2, enabled: true, cleaned: false } }),
      command('succeeded', { kind: 'instance', result: { vmindex: 3, enabled: false, cleaned: true } }),
    ])
    const opOn = ops.start('instance', '2', '启用实例 #2', async () => ({ command_id: 'cmd-1', status: 'pending' }))
    await settle()
    expect(describeOperation(opOn)).toMatchObject({ level: 'success', text: '已启用实例参与录播' })

    const opClean = ops.start('instance', '3', '停用实例 #3', async () => ({ command_id: 'cmd-2', status: 'pending' }))
    await settle()
    expect(describeOperation(opClean)).toMatchObject({ level: 'success', text: '已停用并清理失联实例记录' })

    const opBusy = ops.start('instance', '2', '停用实例 #2', async () => {
      throw new ApiError(409, 'instance_busy', '实例正在占用中，请先停止对应录播任务后再停用')
    })
    await settle()
    expect(describeOperation(opBusy)).toMatchObject({ level: 'error', text: '未受理：实例正在占用中，请先停止对应录播任务后再停用' })
  })
})
