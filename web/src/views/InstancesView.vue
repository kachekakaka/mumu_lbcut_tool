<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { api } from '../api'
import type { Instance } from '../api/types'
import { fmtTime, shortId } from '../format'
import { disableInstanceReason, hostPowerHint } from '../instances'
import { INSTANCE_STATUS, instanceReasonLabel, instanceStatusLabel, instanceStatusType } from '../labels'
import { ops } from '../operations'
import { boolParam, PAGE_SIZES, usePager } from '../pager'
import { canControl, refreshNow, usePageLoader } from '../poller'

const filters = reactive({ status: '', enabled: '' })
const items = ref<Instance[]>([])
const { page, size, total, query, fit } = usePager()

usePageLoader(async () => {
  const p = await api.instances({
    ...query(),
    status: filters.status || undefined,
    enabled: boolParam(filters.enabled),
  })
  items.value = p.items
  fit(p)
})
watch(filters, () => {
  page.value = 1
  void refreshNow()
}, { deep: true })

function toggleEnabled(row: Instance) {
  const next = !row.enabled
  ops.start(
    'instance',
    String(row.vmindex),
    `${next ? '启用' : '停用'}实例：#${row.vmindex} ${row.name || ''}`.trim(),
    (key) => api.setInstanceEnabled(row.vmindex, next, key),
  )
  return false
}
</script>

<template>
  <div>
    <div class="toolbar">
      <el-select v-model="filters.enabled" placeholder="参与录播" clearable size="small" style="width: 120px">
        <el-option label="仅已启用" value="true" />
        <el-option label="仅未启用" value="false" />
      </el-select>
      <el-select v-model="filters.status" placeholder="全部状态" clearable size="small" style="width: 140px">
        <el-option v-for="(label, key) in INSTANCE_STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <span class="muted">勾选“参与录播”后调度器才会向该模拟器派发任务，修改立即持久化</span>
    </div>
    <el-table :data="items" row-key="vmindex" size="small" stripe empty-text="暂无实例">
      <el-table-column prop="vmindex" label="编号" width="70" />
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column label="参与录播" width="95">
        <template #default="{ row }">
          <el-tooltip :disabled="!disableInstanceReason(row)" :content="disableInstanceReason(row)" placement="top">
            <span>
              <el-switch
                :model-value="row.enabled"
                :loading="ops.busy(String(row.vmindex), 'instance')"
                :disabled="!canControl || !!disableInstanceReason(row)"
                :before-change="() => toggleEnabled(row)"
              />
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="150">
        <template #default="{ row }">
          <template v-if="row.enabled || row.status === 'reconcile'">
            <el-tag :type="instanceStatusType(row.status)" size="small">{{ instanceStatusLabel(row.status) }}</el-tag>
          </template>
          <template v-else>
            <el-tag type="info" size="small">未启用</el-tag>
            <span class="muted gap-left">（{{ hostPowerHint(row) }}）</span>
          </template>
        </template>
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
