<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api'
import type { JobDetail, Segment } from '../api/types'
import { fmtDuration, fmtFps, fmtTime } from '../format'
import { endText, heartbeatAge, jobName, STALE_HEARTBEAT_SEC } from '../jobs'
import { eventLabel, identityLabel, identityType, jobStatusLabel, jobStatusType, reasonLabel } from '../labels'
import { connection, errorText, tick, uploadEnabled } from '../poller'
import CopyText from './CopyText.vue'
import SegmentTable from './SegmentTable.vue'

const props = defineProps<{ jobId: string }>()
defineEmits<{ log: [jobId: string] }>()

const job = ref<JobDetail | null>(null)
const segments = ref<Segment[]>([])
const segTotal = ref(0)
const segPage = ref(1)
const SEG_SIZE = 20
const error = ref('')
let busy = false

async function load() {
  if (busy) return
  busy = true
  const jobId = props.jobId
  try {
    const [detail, page] = await Promise.all([
      api.job(jobId),
      api.segments({ job_id: jobId, limit: SEG_SIZE, offset: (segPage.value - 1) * SEG_SIZE }),
    ])
    if (jobId !== props.jobId) return
    job.value = detail
    segments.value = page.items
    segTotal.value = page.total
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy = false
  }
}

watch(() => props.jobId, () => {
  job.value = null
  segments.value = []
  segPage.value = 1
  void load()
}, { immediate: true })
watch([tick, segPage], () => void load())
</script>

<template>
  <div class="job-detail">
    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon />
    <template v-if="job">
      <el-descriptions :column="2" size="small" border>
        <el-descriptions-item label="任务 ID"><CopyText :text="job.id" /></el-descriptions-item>
        <el-descriptions-item label="主播">{{ jobName(job) }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="jobStatusType(job.status)" size="small">{{ jobStatusLabel(job.status) }}</el-tag>
          <span v-if="job.reconcile_requested" class="muted">（已请求对账）</span>
        </el-descriptions-item>
        <el-descriptions-item label="实例">{{ job.vmindex ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ fmtTime(job.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="Worker 拉起">{{ fmtTime(job.started_at) }}</el-descriptions-item>
        <el-descriptions-item label="实际开录">{{ fmtTime(job.recording_started_at) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ fmtTime(job.ended_at) }}</el-descriptions-item>
        <el-descriptions-item label="结束原因">{{ endText(job) }}</el-descriptions-item>
        <el-descriptions-item label="已录时长">{{ fmtDuration(job.elapsed_sec) }}</el-descriptions-item>
        <el-descriptions-item label="帧率">{{ fmtFps(job.fps) }}</el-descriptions-item>
        <el-descriptions-item label="身份核验">
          <el-tag v-if="job.room_identity_status" :type="identityType(job.room_identity_status)" size="small">{{ identityLabel(job.room_identity_status) }}</el-tag>
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="最近心跳">
          {{ fmtTime(job.heartbeat_at) }}
          <span v-if="(heartbeatAge(job, connection.system?.server_time) ?? 0) > STALE_HEARTBEAT_SEC" class="warn-text">
            （{{ heartbeatAge(job, connection.system?.server_time) }} 秒前）
          </span>
        </el-descriptions-item>
      </el-descriptions>

      <div class="section-head">
        <h4>最近事件</h4>
        <el-button size="small" @click="$emit('log', job.id)">查看日志</el-button>
      </div>
      <el-table :data="job.recent_events" size="small" max-height="260" empty-text="暂无事件">
        <el-table-column label="时间" min-width="130">
          <template #default="{ row }">{{ fmtTime(row.timestamp ?? row.received_at) }}</template>
        </el-table-column>
        <el-table-column label="事件" min-width="110">
          <template #default="{ row }">{{ eventLabel(row.event) }}</template>
        </el-table-column>
        <el-table-column label="动作" min-width="90">
          <template #default="{ row }">{{ row.action ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="原因" min-width="140">
          <template #default="{ row }">{{ reasonLabel(row.reason) }}</template>
        </el-table-column>
        <el-table-column label="阶段" min-width="80">
          <template #default="{ row }">{{ row.phase ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="尝试" width="60">
          <template #default="{ row }">{{ row.attempt ?? '—' }}</template>
        </el-table-column>
      </el-table>

      <div class="section-head"><h4>切片（{{ segTotal }}）</h4></div>
      <SegmentTable :items="segments" :upload-enabled="uploadEnabled" :show-job="false" />
      <el-pagination
        v-if="segTotal > SEG_SIZE"
        v-model:current-page="segPage"
        :page-size="SEG_SIZE"
        :total="segTotal"
        layout="prev, pager, next"
        small
      />
    </template>
    <el-skeleton v-else-if="!error" :rows="4" />
  </div>
</template>
