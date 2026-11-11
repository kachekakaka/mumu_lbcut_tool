<script setup lang="ts">
import type { QualityFlag } from '../api/types'
import { fmtTime } from '../format'
import { qualityFlagLabel } from '../labels'

defineProps<{ flags: QualityFlag[] }>()
</script>

<template>
  <span v-if="!flags.length" class="muted">—</span>
  <span v-else class="tag-row">
    <el-tooltip
      v-for="(f, i) in flags"
      :key="i"
      :content="`疑似开始：${fmtTime(f.suspected_start_time)}${f.flag ? `（${f.flag}）` : ''}`"
      placement="top"
    >
      <el-tag type="warning" size="small">{{ qualityFlagLabel(f.flag) }}</el-tag>
    </el-tooltip>
  </span>
</template>
