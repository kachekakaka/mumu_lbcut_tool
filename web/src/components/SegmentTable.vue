<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import type { Segment } from '../api/types'
import { copyText } from '../clipboard'
import { fmtDuration, fmtSize, fmtTime, shortId } from '../format'
import { reasonLabel, segmentStatusLabel, segmentStatusType } from '../labels'
import { connection } from '../poller'
import QualityFlags from './QualityFlags.vue'
import UploadStatus from './UploadStatus.vue'

const props = withDefaults(
  defineProps<{ items: Segment[]; uploadEnabled: boolean; showJob?: boolean; focusJob?: string; focusIndex?: number | null }>(),
  { showJob: true, focusJob: '', focusIndex: null },
)

const rowClass = ({ row }: { row: Segment }) =>
  props.focusJob && row.job_id === props.focusJob && row.segment_index === props.focusIndex ? 'focus-row' : ''

const openingKey = ref<string | null>(null)

async function copyPath(text: string) {
  if (await copyText(text)) {
    ElMessage.success('已复制')
  } else {
    await ElMessageBox.alert(text, '请手动复制', { confirmButtonText: '关闭' }).catch(() => undefined)
  }
}

async function handleOpenFolder(row: Segment) {
  const key = `${row.job_id}:${row.segment_index}`
  if (connection.ok !== true || openingKey.value) return
  openingKey.value = key
  try {
    await api.openSegmentFolder(row.job_id, row.segment_index)
    const isLocal = typeof window !== 'undefined' && ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)
    if (isLocal) {
      ElMessage.success('已发送打开目录请求，请在本机桌面确认')
    } else {
      ElMessage.success('已向录制机发送打开目录请求，请在录制机桌面确认')
    }
  } catch (err: any) {
    ElMessage.error(err?.message || '打开目录失败')
  } finally {
    setTimeout(() => {
      if (openingKey.value === key) {
        openingKey.value = null
      }
    }, 1500)
  }
}
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
    <el-table-column label="文件路径" min-width="280">
      <template #default="{ row }">
        <div v-if="row.file_path" class="segment-file-actions">
          <span class="file-path-text" :title="row.file_path">{{ row.file_path }}</span>
          <div class="action-buttons">
            <el-button link type="primary" size="small" @click.stop="copyPath(row.file_path)">复制</el-button>
            <el-button
              link
              type="primary"
              size="small"
              :loading="openingKey === `${row.job_id}:${row.segment_index}`"
              :disabled="connection.ok !== true || openingKey !== null"
              @click.stop="handleOpenFolder(row)"
            >
              打开目录
            </el-button>
          </div>
        </div>
        <span v-else>—</span>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.segment-file-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  gap: 8px;
}
.file-path-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.action-buttons {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
