<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import type { Segment, Target } from '../api/types'
import { SEGMENT_STATUS } from '../labels'
import { boolParam, PAGE_SIZES, rangeParams, usePager } from '../pager'
import { refreshNow, usePageLoader } from '../poller'
import { arrangementStatus, onLocalConsole, segmentKey, segmentUploads } from '../api/segmentUploads'
import type { Arrangement } from '../api/segmentUploads'
import SegmentTable from '../components/SegmentTable.vue'
import SegmentUploadDialog from '../components/SegmentUploadDialog.vue'

const route = useRoute()
const filters = reactive({
  target_id: String(route.query.target_id ?? ''),
  job_id: String(route.query.job_id ?? ''),
  status: '',
  has_quality_flags: '',
  range: null as [Date, Date] | null,
})
const focusIndex = computed(() => (route.query.focus ? Number(route.query.focus) : null))
const items = ref<Segment[]>([])
const targetOptions = ref<Target[]>([])
const { page, size, total, query, fit } = usePager()
const local = onLocalConsole()
const uploadDialog = ref<InstanceType<typeof SegmentUploadDialog>>()
const uploads = ref<Record<string, Arrangement[]>>({})
let lastUploadLoad = 0
let uploadGeneration = 0

async function loadUploads() {
  if (!local) return
  lastUploadLoad = Date.now()
  const generation = ++uploadGeneration
  const rows = items.value
  if (!rows.length) {
    uploads.value = {}
    return
  }
  try {
    const { items: arrangements } = await segmentUploads.list(rows)
    if (generation !== uploadGeneration) return
    const grouped: Record<string, Arrangement[]> = {}
    for (const record of arrangements) (grouped[segmentKey(record)] ??= []).push(record)
    uploads.value = grouped
  } catch {
    // 投递查询独立于公开切片列表，断线时保留最近结果。
  }
}
onUnmounted(() => { uploadGeneration++ })

usePageLoader(async () => {
  const p = await api.segments({
    ...query(),
    target_id: filters.target_id || undefined,
    job_id: filters.job_id.trim() || undefined,
    status: filters.status || undefined,
    has_quality_flags: boolParam(filters.has_quality_flags),
    ...rangeParams(filters.range),
  })
  const changed = p.items.map(segmentKey).join() !== items.value.map(segmentKey).join()
  items.value = p.items
  fit(p)
  if (changed || Date.now() - lastUploadLoad >= 5000) void loadUploads()
})
watch(filters, () => {
  page.value = 1
  void refreshNow()
}, { deep: true })

async function loadTargets() {
  try {
    targetOptions.value = (await api.targets({ limit: 200 })).items
  } catch {
    // 下拉选项读取失败不影响切片列表
  }
}
onMounted(loadTargets)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-select v-model="filters.target_id" placeholder="全部主播" clearable filterable size="small" style="width: 160px" @visible-change="(v: boolean) => v && loadTargets()">
        <el-option v-for="t in targetOptions" :key="t.id" :label="t.nickname || t.raw" :value="t.id" />
      </el-select>
      <el-input v-model="filters.job_id" placeholder="任务 ID" clearable size="small" style="width: 200px" />
      <el-select v-model="filters.status" placeholder="全部状态" clearable size="small" style="width: 110px">
        <el-option v-for="(label, key) in SEGMENT_STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.has_quality_flags" placeholder="质量标记" clearable size="small" style="width: 120px">
        <el-option label="有质量标记" value="true" />
        <el-option label="无质量标记" value="false" />
      </el-select>
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        size="small"
        start-placeholder="开始时间起"
        end-placeholder="止"
        style="width: 320px"
      />
    </div>
    <SegmentTable :items="items" :upload-enabled="false" :focus-job="filters.job_id" :focus-index="focusIndex">
      <template v-if="local" #upload="{ row }">
        <template v-if="uploads[segmentKey(row)]?.length">
          <div v-for="record in uploads[segmentKey(row)]" :key="record.id">
            <span>{{ arrangementStatus(record) }}</span>
            <el-button link type="primary" size="small" @click="uploadDialog?.show(row, record)">查看任务</el-button>
          </div>
          <el-button v-if="row.status === 'finished'" link size="small" @click="uploadDialog?.show(row)">安排其他账号</el-button>
        </template>
        <el-button v-else-if="row.status === 'finished' && row.started_at && row.file_path" link type="primary" size="small" @click="uploadDialog?.show(row)">安排上传</el-button>
        <span v-else>—</span>
      </template>
    </SegmentTable>
    <SegmentUploadDialog v-if="local" ref="uploadDialog" @changed="loadUploads" />
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="PAGE_SIZES" layout="total, sizes, prev, pager, next" small />
  </div>
</template>
