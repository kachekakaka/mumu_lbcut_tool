<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { api } from '../api'
import type { Instance } from '../api/types'
import { fmtTime, shortId } from '../format'
import { INSTANCE_STATUS, instanceReasonLabel, instanceStatusLabel, instanceStatusType } from '../labels'
import { refreshNow, usePageLoader } from '../poller'
import { PAGE_SIZES, usePager } from '../pager'

const filters = reactive({ status: '' })
const items = ref<Instance[]>([])
const { page, size, total, query, fit } = usePager()

usePageLoader(async () => {
  const p = await api.instances({ ...query(), status: filters.status || undefined })
  items.value = p.items
  fit(p)
})
watch(filters, () => {
  page.value = 1
  void refreshNow()
})
</script>

<template>
  <div>
    <div class="toolbar">
      <el-select v-model="filters.status" placeholder="全部状态" clearable size="small" style="width: 140px">
        <el-option v-for="(label, key) in INSTANCE_STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <span class="muted">实例只查看，不提供操作</span>
    </div>
    <el-table :data="items" size="small" stripe empty-text="暂无实例">
      <el-table-column prop="vmindex" label="编号" width="70" />
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><el-tag :type="instanceStatusType(row.status)" size="small">{{ instanceStatusLabel(row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="当前任务" min-width="160">
        <template #default="{ row }">
          <router-link v-if="row.job_id" :to="{ path: '/jobs', query: { job: row.job_id } }">{{ shortId(row.job_id) }}</router-link>
          <span v-if="row.job_id" class="gap-left">{{ row.nickname || row.target_id || '' }}</span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="原因" min-width="130">
        <template #default="{ row }">{{ instanceReasonLabel(row.reason) }}</template>
      </el-table-column>
      <el-table-column label="状态更新时间" min-width="130">
        <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
      </el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="PAGE_SIZES" layout="total, sizes, prev, pager, next" small />
  </div>
</template>
