<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import type { Segment, Target } from '../api/types'
import { SEGMENT_STATUS } from '../labels'
import { boolParam, PAGE_SIZES, rangeParams, usePager } from '../pager'
import { refreshNow, uploadEnabled, usePageLoader } from '../poller'
import { UPLOAD_STATUSES, uploadStatusLabel } from '../upload'
import SegmentTable from '../components/SegmentTable.vue'

const route = useRoute()
const filters = reactive({
  target_id: String(route.query.target_id ?? ''),
  job_id: String(route.query.job_id ?? ''),
  status: '',
  has_quality_flags: '',
  upload_status: '',
  range: null as [Date, Date] | null,
})
const focusIndex = computed(() => (route.query.focus ? Number(route.query.focus) : null))
const items = ref<Segment[]>([])
const targetOptions = ref<Target[]>([])
const { page, size, total, query, fit } = usePager()

usePageLoader(async () => {
  const p = await api.segments({
    ...query(),
    target_id: filters.target_id || undefined,
    job_id: filters.job_id.trim() || undefined,
    status: filters.status || undefined,
    has_quality_flags: boolParam(filters.has_quality_flags),
    // 上传筛选只在后端声明启用上传时发送；当前真实后端始终未启用。
    upload_status: uploadEnabled.value ? filters.upload_status || undefined : undefined,
    ...rangeParams(filters.range),
  })
  items.value = p.items
  fit(p)
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
      <el-select v-if="uploadEnabled" v-model="filters.upload_status" placeholder="上传状态" clearable size="small" style="width: 120px">
        <el-option v-for="s in UPLOAD_STATUSES" :key="s" :label="uploadStatusLabel(s)" :value="s" />
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
    <SegmentTable :items="items" :upload-enabled="uploadEnabled" :focus-job="filters.job_id" :focus-index="focusIndex" />
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="PAGE_SIZES" layout="total, sizes, prev, pager, next" small />
  </div>
</template>
