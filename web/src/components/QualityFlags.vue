<script setup lang="ts">
import { computed } from 'vue'
import type { QualityFlag } from '../api/types'
import { fmtTime } from '../format'
import { qualityFlagLabel } from '../labels'

const props = defineProps<{ flags: QualityFlag[] }>()

interface GroupedFlag {
  flag: string | null
  count: number
  firstTime: number | null
  lastTime: number | null
  tooltip: string
}

const groupedFlags = computed<GroupedFlag[]>(() => {
  if (!props.flags || !props.flags.length) return []
  const map = new Map<string, { flag: string | null; times: number[]; count: number }>()

  for (const f of props.flags) {
    const key = f.flag ?? '__null__'
    let item = map.get(key)
    if (!item) {
      item = { flag: f.flag, times: [], count: 0 }
      map.set(key, item)
    }
    item.count++
    if (f.suspected_start_time != null) {
      item.times.push(f.suspected_start_time)
    }
  }

  return Array.from(map.values()).map((item) => {
    item.times.sort((a, b) => a - b)
    const firstTime = item.times.length ? item.times[0] : null
    const lastTime = item.times.length ? item.times[item.times.length - 1] : null
    const code = item.flag ? `（${item.flag}）` : ''

    let tooltip = ''
    if (item.count > 1) {
      if (firstTime != null && lastTime != null && firstTime !== lastTime) {
        tooltip = `发生 ${item.count} 次，首次：${fmtTime(firstTime)}，最近：${fmtTime(lastTime)}${code}`
      } else if (firstTime != null) {
        tooltip = `发生 ${item.count} 次，疑似开始：${fmtTime(firstTime)}${code}`
      } else {
        tooltip = `发生 ${item.count} 次${code}`
      }
    } else {
      tooltip = `疑似开始：${fmtTime(firstTime)}${code}`
    }

    return {
      flag: item.flag,
      count: item.count,
      firstTime,
      lastTime,
      tooltip,
    }
  })
})
</script>

<template>
  <span v-if="!groupedFlags.length" class="muted">—</span>
  <span v-else class="tag-row">
    <el-tooltip
      v-for="(g, i) in groupedFlags"
      :key="i"
      :content="g.tooltip"
      placement="top"
    >
      <el-tag type="warning" size="small">
        {{ qualityFlagLabel(g.flag) }}{{ g.count > 1 ? ` × ${g.count}` : '' }}
      </el-tag>
    </el-tooltip>
  </span>
</template>
