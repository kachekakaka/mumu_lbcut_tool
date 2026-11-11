import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from './api'
import { ApiError, isDisconnect } from './api/client'
import type { SystemInfo } from './api/types'
import { errorCodeLabel } from './labels'

export type Loader = () => Promise<void>

export const connection = reactive({
  ok: null as boolean | null,
  lastSuccessAt: null as number | null,
  system: null as SystemInfo | null,
})
export const pageError = ref('')
/** 每轮成功刷新后递增，供页面内的附属区域（如任务详情）跟随刷新。 */
export const tick = ref(0)

const VISIBLE_MS = 2000
const HIDDEN_MS = 10000

let loader: Loader | null = null
let running = false
let rerun = false
let timer: ReturnType<typeof setTimeout> | null = null
let started = false

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const label = errorCodeLabel(error.code)
    return error.message && error.message !== label ? `${label}（${error.message}）` : label
  }
  return String(error)
}

async function runOnce() {
  const results = await Promise.allSettled([api.system(), loader ? loader() : Promise.resolve()])
  const [system, page] = results
  if (system.status === 'fulfilled') connection.system = system.value
  const failures = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected').map((r) => r.reason)
  if (failures.some(isDisconnect)) {
    connection.ok = false
    return
  }
  connection.ok = true
  connection.lastSuccessAt = Date.now() / 1000
  pageError.value = page.status === 'rejected' ? errorText(page.reason) : ''
  tick.value++
}

function schedule() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => void refreshNow(), document.hidden ? HIDDEN_MS : VISIBLE_MS)
}

/** 立即刷新；上一轮未完成时不叠加请求，结束后再补一轮。 */
export async function refreshNow(): Promise<void> {
  if (running) {
    rerun = true
    return
  }
  running = true
  if (timer) clearTimeout(timer)
  try {
    do {
      rerun = false
      await runOnce()
    } while (rerun)
  } finally {
    running = false
    if (started) schedule()
  }
}

export function startPolling() {
  if (started) return
  started = true
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void refreshNow()
  })
  void refreshNow()
}

/** 注册当前页面的数据读取；同一时间只有一个页面读取在轮询中。 */
export function usePageLoader(fn: Loader) {
  onMounted(() => {
    loader = fn
    pageError.value = ''
    void refreshNow()
  })
  onUnmounted(() => {
    if (loader === fn) loader = null
  })
}

/** 只有连接正常且调度服务在运行时才允许提交操作。 */
export const canControl = computed(
  () => connection.ok === true && connection.system?.service_state === 'running' && connection.system.scheduler_running === true,
)

export const uploadEnabled = computed(() => connection.system?.upload_enabled === true)
