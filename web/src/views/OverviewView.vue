<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import type { Alert, Instance, Overview } from '../api/types'
import { fmtClock, fmtTime, shortId } from '../format'
import { alertTypeLabel, alertTypeType, instanceReasonLabel, instanceStatusLabel, instanceStatusType, reasonLabel } from '../labels'
import { usePageLoader } from '../poller'
import { PAGE_SIZES, usePager } from '../pager'

const router = useRouter()
const overview = ref<Overview | null>(null)
const instances = ref<Instance[]>([])
const { page, size, total, query, fit } = usePager(20)

usePageLoader(async () => {
  const [o, i] = await Promise.all([
    api.overview(query()),
    api.instances({ limit: 200 }),
  ])
  overview.value = o
  instances.value = i.items
  fit(o.alerts)
})

const counts = computed(() => overview.value?.instance_counts ?? {})

function objectName(a: Alert): string {
  const name = a.nickname || a.target_raw || (a.target_id ? `主播 ${shortId(a.target_id)}` : '')
  if (a.object_type === 'segment') return `${name} · 任务 ${shortId(a.job_id)} 第 ${a.segment_index} 片`
  if (a.object_type === 'job') return `${name} · 任务 ${shortId(a.job_id)}`
  return name || '—'
}

function detail(a: Alert): string {
  if (a.type === 'retrying') {
    const next = (a.retry_at ?? 0) > (overview.value?.generated_at ?? 0) ? `下次 ${fmtClock(a.retry_at)}` : '等待重试'
    return `第 ${a.attempts ?? '—'} 次，${next}；${reasonLabel(a.reason)}`
  }
  return reasonLabel(a.reason)
}

function open(a: Alert) {
  if (a.type === 'retrying' || a.type === 'needs_fix') {
    router.push({ path: '/targets', query: { notice: 'true', focus: a.target_id ?? '' } })
  } else if (a.object_type === 'segment' && a.job_id) {
    router.push({ path: '/segments', query: { job_id: a.job_id, focus: String(a.segment_index ?? '') } })
  } else if (a.job_id) {
    router.push({ path: '/jobs', query: { job: a.job_id } })
  }
}
</script>

<template>
  <div>
    <div class="cards">
      <el-card shadow="never"><div class="metric-label">正在录制</div><div class="metric">{{ overview?.recording_count ?? '—' }}</div></el-card>
      <el-card shadow="never"><div class="metric-label">排队任务</div><div class="metric">{{ overview?.pending_count ?? '—' }}</div></el-card>
      <el-card shadow="never">
        <div class="metric-label">空闲 / 离线实例</div>
        <div class="metric">{{ counts.idle ?? '—' }} / {{ counts.offline ?? '—' }}</div>
        <div v-if="counts.reconcile" class="warn-text">待对账 {{ counts.reconcile }}</div>
      </el-card>
      <el-card shadow="never">
        <div class="metric-label">今日完成切片</div>
        <div class="metric">{{ overview?.today_finished_segments ?? '—' }}</div>
        <div class="muted">按北京时间当日计（{{ overview?.statistics_timezone ?? 'Asia/Shanghai' }}）</div>
      </el-card>
    </div>

    <h3>实例</h3>
    <div class="instance-strip">
      <router-link
        v-for="i in instances"
        :key="i.vmindex"
        class="instance-card"
        :to="i.job_id ? { path: '/jobs', query: { job: i.job_id } } : '/instances'"
      >
        <div><strong>#{{ i.vmindex }}</strong> {{ i.name }}</div>
        <el-tag :type="instanceStatusType(i.status)" size="small">{{ instanceStatusLabel(i.status) }}</el-tag>
        <div class="muted">{{ i.nickname || (i.reason ? instanceReasonLabel(i.reason) : '—') }}</div>
      </router-link>
      <span v-if="!instances.length" class="muted">暂无实例</span>
    </div>

    <h3>告警（{{ total }}）</h3>
    <el-table :data="overview?.alerts.items ?? []" size="small" empty-text="暂无告警" @row-click="open">
      <el-table-column label="时间" min-width="130">
        <template #default="{ row }">{{ fmtTime(row.timestamp) }}</template>
      </el-table-column>
      <el-table-column label="类型" min-width="110">
        <template #default="{ row }"><el-tag :type="alertTypeType(row.type)" size="small">{{ alertTypeLabel(row.type) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="对象" min-width="200">
        <template #default="{ row }">{{ objectName(row) }}</template>
      </el-table-column>
      <el-table-column label="说明" min-width="240">
        <template #default="{ row }">{{ detail(row) }}</template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      v-model:page-size="size"
      :total="total"
      :page-sizes="PAGE_SIZES"
      layout="total, sizes, prev, pager, next"
      small
    />
  </div>
</template>
