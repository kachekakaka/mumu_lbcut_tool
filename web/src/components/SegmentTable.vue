<script setup lang="ts">
import type { Segment } from '../api/types'
import { fmtDuration, fmtSize, fmtTime, shortId } from '../format'
import { reasonLabel, segmentStatusLabel, segmentStatusType } from '../labels'
import CopyText from './CopyText.vue'
import QualityFlags from './QualityFlags.vue'
import UploadStatus from './UploadStatus.vue'

const props = withDefaults(
  defineProps<{ items: Segment[]; uploadEnabled: boolean; showJob?: boolean; focusJob?: string; focusIndex?: number | null }>(),
  { showJob: true, focusJob: '', focusIndex: null },
)

const rowClass = ({ row }: { row: Segment }) =>
  props.focusJob && row.job_id === props.focusJob && row.segment_index === props.focusIndex ? 'focus-row' : ''
</script>

<template>
  <el-table :data="items" :row-key="(r: Segment) => `${r.job_id}:${r.segment_index}`" :row-class-name="rowClass" size="small" stripe>
    <el-table-column v-if="showJob" label="主播" min-width="120">
      <template #default="{ row }">{{ row.nickname || row.target_raw || row.target_id }}</template>
    </el-table-column>
    <el-table-column v-if="showJob" label="任务" min-width="90">
      <template #default="{ row }">
        <router-link :to="{ path: '/jobs', query: { job: row.job_id } }">{{ shortId(row.job_id) }}</router-link>
      </template>
    </el-table-column>
    <el-table-column prop="segment_index" label="序号" width="60" />
    <el-table-column label="状态" width="80">
      <template #default="{ row }">
        <el-tag :type="segmentStatusType(row.status)" size="small">{{ segmentStatusLabel(row.status) }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="开始时间" min-width="130">
      <template #default="{ row }">{{ fmtTime(row.started_at ?? row.created_at) }}</template>
    </el-table-column>
    <el-table-column label="时长" width="80">
      <template #default="{ row }">{{ fmtDuration(row.duration_sec) }}</template>
    </el-table-column>
    <el-table-column label="大小" width="90">
      <template #default="{ row }">{{ fmtSize(row.file_size_mb) }}</template>
    </el-table-column>
    <el-table-column label="末片" width="60">
      <template #default="{ row }">{{ row.is_final ? '是' : '否' }}</template>
    </el-table-column>
    <el-table-column label="质量标记" min-width="140">
      <template #default="{ row }"><QualityFlags :flags="row.quality_flags" /></template>
    </el-table-column>
    <el-table-column label="中止原因" min-width="110">
      <template #default="{ row }">{{ row.status === 'aborted' ? reasonLabel(row.reason) : '—' }}</template>
    </el-table-column>
    <el-table-column v-if="uploadEnabled" label="上传状态" min-width="100">
      <template #default="{ row }"><UploadStatus :upload="row.upload" /></template>
    </el-table-column>
    <el-table-column label="文件路径" min-width="260">
      <template #default="{ row }"><CopyText :text="row.file_path" /></template>
    </el-table-column>
  </el-table>
</template>
