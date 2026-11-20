import { reactive } from 'vue'
import { ApiError } from './api/client'
import type { Command, Receipt } from './api/types'
import {
  RECORD_OUTCOME, STOP_JOB_OUTCOME, STOP_TARGET_OUTCOME, commandKindLabel, errorCodeLabel,
} from './labels'

/**
 * 操作状态：submitting/unknown/rejected 是网页侧状态（尚未拿到或未取得命令），
 * 其余取后端命令状态；未知命令状态原样保存。
 */
export type OperationState = string

export interface Operation {
  seq: number
  kind: string
  subject: string
  title: string
  requestId: string
  state: OperationState
  commandId: string | null
  command: Command | null
  errorCode: string | null
  errorMessage: string | null
  createdAt: number
  followError: boolean
}

export type Sender = (requestId: string) => Promise<Receipt>
export type DoneCallback = (op: Operation) => void

export interface OperationDeps {
  getCommand: (commandId: string, signal: AbortSignal) => Promise<Command>
  newId: () => string
  refresh: () => void
  sleep?: (ms: number) => Promise<void>
  notify?: (op: Operation) => void
  pollMs?: number
  now?: () => number
}

const FOLLOWING = ['pending', 'running']
const ACTIVE = ['submitting', ...FOLLOWING]

export const isActiveOperation = (op: Operation) => ACTIVE.includes(op.state)

export function createOperations(deps: OperationDeps) {
  const list = reactive<Operation[]>([])
  const senders = new Map<number, Sender>()
  const callbacks = new Map<number, { run: DoneCallback; owner?: symbol }>()
  const tracking = new Map<number, AbortController>()
  const pollMs = deps.pollMs ?? 1000
  let seq = 0

  function release(op: Operation) {
    tracking.get(op.seq)?.abort()
    tracking.delete(op.seq)
    senders.delete(op.seq)
    callbacks.delete(op.seq)
  }

  function finish(op: Operation) {
    const callback = callbacks.get(op.seq)
    release(op)
    let count = 0
    for (let i = 0; i < list.length; i++) {
      if (!tracking.has(list[i].seq) && ++count > 30) list.splice(i--, 1)
    }
    deps.refresh()
    deps.notify?.(op)
    callback?.run(op)
  }

  function sleep(ms: number, signal: AbortSignal) {
    if (deps.sleep) return deps.sleep(ms)
    return new Promise<void>((resolve) => {
      const complete = () => {
        clearTimeout(timer)
        signal.removeEventListener('abort', complete)
        resolve()
      }
      const timer = setTimeout(complete, ms)
      signal.addEventListener('abort', complete, { once: true })
    })
  }

  async function follow(op: Operation, signal: AbortSignal) {
    while (op.commandId && !signal.aborted) {
      try {
        const command = await deps.getCommand(op.commandId, signal)
        if (signal.aborted) return
        op.command = command
        op.state = command.status
        op.followError = false
        if (!FOLLOWING.includes(command.status)) break
      } catch (error) {
        if (signal.aborted) return
        if (error instanceof ApiError && error.code === 'command_not_found') {
          op.state = 'unknown'
          op.errorCode = error.code
          op.errorMessage = error.message
          return
        }
        op.followError = true
      }
      await sleep(op.followError ? pollMs * 2 : pollMs, signal)
    }
    if (!signal.aborted) finish(op)
  }

  async function submit(op: Operation) {
    const controller = tracking.get(op.seq)!
    op.state = 'submitting'
    op.errorCode = null
    op.errorMessage = null
    let receipt: Receipt
    try {
      receipt = await senders.get(op.seq)!(op.requestId)
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof ApiError && error.code !== 'network_error') {
        op.state = 'rejected'
        op.errorCode = error.code
        op.errorMessage = error.message
      } else {
        // 请求可能已到达后端：保留请求编号，只允许用同一编号重试。
        op.state = 'unknown'
        op.errorCode = 'network_error'
        op.errorMessage = '网络中断，无法确认是否已提交'
      }
      if (op.state === 'rejected') finish(op)
      else deps.notify?.(op)
      return
    }
    if (controller.signal.aborted) return
    senders.delete(op.seq)
    op.commandId = receipt.command_id
    op.state = receipt.status
    await follow(op, controller.signal)
  }

  function start(kind: string, subject: string, title: string, send: Sender, onDone?: DoneCallback, owner?: symbol): Operation {
    list.unshift({
      seq: ++seq, kind, subject, title, requestId: deps.newId(), state: 'submitting', commandId: null, command: null,
      errorCode: null, errorMessage: null, createdAt: (deps.now ?? Date.now)() / 1000, followError: false,
    })
    const op = list[0]
    tracking.set(op.seq, new AbortController())
    senders.set(op.seq, send)
    if (onDone) callbacks.set(op.seq, { run: onDone, owner })
    void submit(op)
    return op
  }

  /** 结果不明时用原请求编号重试；后端按编号返回原命令，不会重复执行。 */
  function retry(op: Operation): boolean {
    if (op.state !== 'unknown' || op.commandId || !senders.has(op.seq)) return false
    void submit(op)
    return true
  }

  function busy(subject: string, kind?: string): boolean {
    return list.some((op) => op.subject === subject && (!kind || op.kind === kind) && isActiveOperation(op))
  }

  function endTracking(op: Operation) {
    if (op.state !== 'unknown' && !op.followError) return
    release(op)
    const index = list.findIndex((item) => item.seq === op.seq)
    if (index >= 0) list.splice(index, 1)
  }

  function detachOwner(owner: symbol) {
    for (const [seq, callback] of callbacks) {
      if (callback.owner === owner) callbacks.delete(seq)
    }
  }

  function dispose() {
    for (const op of list) release(op)
    list.splice(0)
  }

  return { list, start, retry, busy, endTracking, detachOwner, dispose }
}

export type Level = 'success' | 'info' | 'warning' | 'error'

export interface Description {
  level: Level
  text: string
  notes: string[]
}

function outcomeText(map: Record<string, string>, value: unknown): string {
  return typeof value === 'string' ? map[value] ?? value : '已执行'
}

/** 把操作状态转成用户能看懂的结论；只依据状态与稳定代码，不解析后端中文说明。 */
export function describeOperation(op: Operation): Description {
  const result = op.command?.result ?? null
  switch (op.state) {
    case 'submitting':
      return { level: 'info', text: '提交中…', notes: [] }
    case 'unknown':
      return {
        level: 'warning',
        text: op.commandId ? '命令结果暂时无法查询' : '网络中断，无法确认是否已提交',
        notes: op.commandId ? [] : ['可用同一请求编号重试，不会重复执行'],
      }
    case 'rejected':
      return { level: 'error', text: `未受理：${errorCodeLabel(op.errorCode)}`, notes: op.errorMessage ? [op.errorMessage] : [] }
    case 'pending':
      return { level: 'info', text: op.followError ? '已提交，正在重新连接以查询结果' : '已提交，等待执行', notes: [] }
    case 'running':
      return { level: 'info', text: op.followError ? '执行中，正在重新连接以查询结果' : '执行中', notes: [] }
    case 'interrupted':
      return { level: 'warning', text: '服务重启，命令已中断', notes: ['请重新操作（会生成新的请求编号）'] }
    case 'failed':
      return {
        level: 'error',
        text: `失败：${errorCodeLabel(op.command?.error_code ?? null)}`,
        notes: op.command?.message ? [op.command.message] : [],
      }
    case 'succeeded':
      return describeSuccess(op.kind, result)
    default:
      return { level: 'info', text: `命令状态：${op.state}`, notes: [] }
  }
}

function describeSuccess(kind: string, result: Record<string, any> | null): Description {
  const notes: string[] = []
  switch (kind) {
    case 'add': {
      const items: { outcome?: string }[] = Array.isArray(result?.items) ? result!.items : []
      const count = (o: string) => items.filter((i) => i.outcome === o).length
      const failed = items.filter((i) => !['created', 'existing', 'restored'].includes(String(i.outcome))).length
      const parts = [`新增 ${count('created')}`, `已存在 ${count('existing')}`, `已恢复 ${count('restored')}`, `失败 ${failed}`]
      return { level: failed ? 'warning' : 'success', text: failed ? `部分失败：${parts.join('，')}` : parts.join('，'), notes }
    }
    case 'record':
      if (result?.outcome === 'queued') notes.push('实际开录请看任务状态')
      return { level: 'success', text: outcomeText(RECORD_OUTCOME, result?.outcome), notes }
    case 'stop_target': {
      const reconcile: string[] = Array.isArray(result?.reconcile_job_ids) ? result!.reconcile_job_ids : []
      if (reconcile.length) notes.push(`后续重试已取消，遗留进程仍需对账（${reconcile.length} 个任务）`)
      if (result?.outcome === 'stop_requested') notes.push('实际封口请看任务和切片状态')
      return { level: reconcile.length ? 'warning' : 'success', text: outcomeText(STOP_TARGET_OUTCOME, result?.outcome), notes }
    }
    case 'stop':
      if (result?.outcome === 'stop_requested') notes.push('实际封口请看任务和切片状态')
      return { level: 'success', text: outcomeText(STOP_JOB_OUTCOME, result?.outcome), notes }
    case 'remove':
      return { level: 'success', text: '已停止监控', notes: ['正在录制的任务会平滑停止，录像保留'] }
    case 'update':
      return { level: 'success', text: result?.auto_record ? '自动录制已开启' : '自动录制已关闭', notes }
    case 'instance':
      if (result?.cleaned) return { level: 'success', text: '已停用并清理失联实例记录', notes }
      return { level: 'success', text: result?.enabled ? '已启用实例参与录播' : '已停用实例', notes }
    default:
      return { level: 'success', text: `${commandKindLabel(kind)}已执行`, notes }
  }
}
