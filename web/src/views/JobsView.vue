<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import type { Job, Target } from '../api/types'
import { fmtDuration, fmtFps, fmtTime, shortId } from '../format'
import { endText, heartbeatAge, jobName, STALE_HEARTBEAT_SEC } from '../jobs'
import {
  ACTIVE_JOB_STATUSES, HISTORY_JOB_STATUSES, STOPPABLE_JOB_STATUSES, identityLabel, identityType, jobStatusLabel, jobStatusType,
} from '../labels'
import { useMobile } from '../mobile'
import { PAGE_SIZES, rangeParams, usePager } from '../pager'
import { canControl, connection, refreshNow, usePageLoader } from '../poller'
import { ops } from '../operations'
import CopyText from '../components/CopyText.vue'
import JobDetail from '../components/JobDetail.vue'
import LogDrawer from '../components/LogDrawer.vue'

const route = useRoute()
const router = useRouter()
const mobile = useMobile()
const filters = reactive({
  active: route.query.active === 'false' ? 'false' : 'true',
  target_id: String(route.query.target_id ?? ''),
  status: '',
  range: null as [Date, Date] | null,
})
const items = ref<Job[]>([])
const targetOptions = ref<Target[]>([])
const { page, size, total, query, fit } = usePager()
const statusOptions = computed(() => (filters.active === 'true' ? ACTIVE_JOB_STATUSES : HISTORY_JOB_STATUSES))

usePageLoader(async () => {
  const p = await api.jobs({
    ...query(),
    active: filters.active === 'true',
    target_id: filters.target_id || undefined,
    status: filters.status || undefined,
    ...rangeParams(filters.range),
  })
  items.value = p.items
  fit(p)
})
watch(() => filters.active, () => {
  filters.status = ''
})
watch(filters, () => {
  page.value = 1
  void refreshNow()
}, { deep: true })

async function loadTargets() {
  try {
    targetOptions.value = (await api.targets({ limit: 200 })).items
  } catch {
    // 下拉选项读取失败不影响任务列表
  }
}
onMounted(loadTargets)

const detailId = computed(() => (typeof route.query.job === 'string' ? route.query.job : null))
const detailOpen = computed({
  get: () => !!detailId.value,
  set: (open: boolean) => {
    if (!open) void router.replace({ path: '/jobs', query: { ...route.query, job: undefined } })
  },
})
const openDetail = (job: Job) => router.replace({ path: '/jobs', query: { ...route.query, job: job.id } })

const logOpen = ref(false)
const logJob = ref<string | null>(null)
function openLog(jobId: string) {
  logJob.value = jobId
  logOpen.value = true
}

function staleSeconds(job: Job): number | null {
  if (job.status !== 'running') return null
  const age = heartbeatAge(job, connection.system?.server_time)
  return age !== null && age > STALE_HEARTBEAT_SEC ? age : null
}

async function stop(job: Job) {
  try {
    await ElMessageBox.confirm(
      `确认平滑停止「${jobName(job)}」的任务？当前切片会封口保存，本场不再自动开录，可在主播页立即开录恢复。`,
      '平滑停止',
      { type: 'warning', confirmButtonText: '平滑停止', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  ops.start('stop', job.id, `平滑停止：${jobName(job)}`, (key) => api.stopJob(job.id, key))
}
</script>

<template>
  <div>
    <div class="toolbar">
      <el-radio-group v-model="filters.active" size="small">
        <el-radio-button value="true">进行中</el-radio-button>
        <el-radio-button value="false">历史</el-radio-button>
      </el-radio-group>
      <el-select v-model="filters.target_id" placeholder="全部主播" clearable filterable size="small" style="width: 160px" @visible-change="(v: boolean) => v && loadTargets()">
        <el-option v-for="t in targetOptions" :key="t.id" :label="t.nickname || t.raw" :value="t.id" />
      </el-select>
      <el-select v-model="filters.status" placeholder="全部状态" clearable size="small" style="width: 130px">
        <el-option v-for="s in statusOptions" :key="s" :label="jobStatusLabel(s)" :value="s" />
      </el-select>
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        size="small"
        start-placeholder="创建时间起"
        end-placeholder="止"
        style="width: 320px"
      />
    </div>
    <el-table :data="items" row-key="id" size="small" stripe empty-text="暂无任务">
      <el-table-column label="任务 ID" min-width="120">
        <template #default="{ row }"><CopyText :text="row.id" :display="shortId(row.id)" /></template>
      </el-table-column>
      <el-table-column label="主播" min-width="120">
        <template #default="{ row }">{{ jobName(row) }}</template>
      </el-table-column>
      <el-table-column label="实例" width="60">
        <template #default="{ row }">{{ row.vmindex ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" min-width="100">
        <template #default="{ row }">
          <el-tag :type="jobStatusType(row.status)" size="small">{{ jobStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="实际开录" min-width="130">
        <template #default="{ row }">{{ fmtTime(row.recording_started_at) }}</template>
      </el-table-column>
      <el-table-column label="已录时长" min-width="100">
        <template #default="{ row }">
          {{ fmtDuration(row.elapsed_sec) }}
          <div v-if="staleSeconds(row) !== null" class="warn-text">心跳 {{ staleSeconds(row) }} 秒前</div>
        </template>
      </el-table-column>
      <el-table-column label="帧率" width="70">
        <template #default="{ row }">{{ fmtFps(row.fps) }}</template>
      </el-table-column>
      <el-table-column label="身份核验" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.room_identity_status" :type="identityType(row.room_identity_status)" size="small">{{ identityLabel(row.room_identity_status) }}</el-tag>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="切片 完成/中止" min-width="100">
        <template #default="{ row }">{{ row.finished_segment_count }} / {{ row.aborted_segment_count }}</template>
      </el-table-column>
      <el-table-column label="中断、恢复与停止" min-width="220">
        <template #default="{ row }">{{ endText(row) }}</template>
      </el-table-column>
      <el-table-column label="操作" min-width="200" :fixed="mobile ? false : 'right'">
        <template #default="{ row }">
          <el-button size="small" @click="openDetail(row)">详情</el-button>
          <el-button size="small" @click="openLog(row.id)">日志</el-button>
          <el-button
            v-if="STOPPABLE_JOB_STATUSES.includes(row.status)"
            size="small"
            type="warning"
            :disabled="!canControl"
            :loading="ops.busy(row.id, 'stop')"
            @click="stop(row)"
          >
            平滑停止
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="PAGE_SIZES" layout="total, sizes, prev, pager, next" small />

    <el-drawer v-model="detailOpen" :title="`任务详情 ${shortId(detailId)}`" :size="mobile ? '100%' : '70%'">
      <JobDetail v-if="detailId" :job-id="detailId" @log="openLog" />
    </el-drawer>
    <LogDrawer v-model="logOpen" :job-id="logJob" />
  </div>
</template>
