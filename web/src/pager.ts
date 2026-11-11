import { ref, watch } from 'vue'
import type { Page } from './api/types'
import { refreshNow } from './poller'

export const PAGE_SIZES = [20, 50, 100, 200]

export function usePager(defaultSize = 50) {
  const page = ref(1)
  const size = ref(defaultSize)
  const total = ref(0)
  const query = () => ({ limit: size.value, offset: (page.value - 1) * size.value })
  /** 数据减少导致当前页越界时退回最后一页。 */
  function fit(result: Page<unknown>) {
    total.value = result.total
    if (result.items.length === 0 && result.total > 0 && page.value > 1) {
      page.value = Math.max(1, Math.ceil(result.total / size.value))
    }
  }
  watch([page, size], () => void refreshNow())
  return { page, size, total, query, fit }
}

export function boolParam(value: string): boolean | undefined {
  return value === 'true' ? true : value === 'false' ? false : undefined
}

export function rangeParams(range: [Date, Date] | null | undefined) {
  if (!range) return {}
  return { from_time: range[0].getTime() / 1000, to_time: range[1].getTime() / 1000 }
}
