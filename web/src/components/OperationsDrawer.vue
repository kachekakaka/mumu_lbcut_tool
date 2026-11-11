<script setup lang="ts">
import { describeOperation } from '../commands'
import { fmtTime, shortId } from '../format'
import { addOutcomeLabel, addOutcomeType, commandStatusLabel, commandStatusType } from '../labels'
import { useMobile } from '../mobile'
import { canControl } from '../poller'
import { ops } from '../operations'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const mobile = useMobile()
const levelType = { success: 'success', info: 'info', warning: 'warning', error: 'error' } as const
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    title="操作记录"
    :size="mobile ? '100%' : '480px'"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <p class="muted">“已执行”只表示调度器接受了操作；实际开录、封口请以任务和切片状态为准。</p>
    <el-empty v-if="!ops.list.length" description="本页面打开后还没有操作" />
    <el-card v-for="op in ops.list" :key="op.seq" class="op-card" shadow="never">
      <div class="op-head">
        <strong>{{ op.title }}</strong>
        <el-tag :type="commandStatusType(op.state)" size="small">{{ commandStatusLabel(op.state) }}</el-tag>
      </div>
      <el-alert
        :type="levelType[describeOperation(op).level]"
        :title="describeOperation(op).text"
        :closable="false"
        show-icon
      >
        <div v-for="(n, i) in describeOperation(op).notes" :key="i">{{ n }}</div>
      </el-alert>
      <el-table v-if="op.kind === 'add' && Array.isArray(op.command?.result?.items)" :data="op.command!.result!.items" size="small">
        <el-table-column prop="input" label="输入" min-width="160" show-overflow-tooltip />
        <el-table-column label="结果" min-width="110">
          <template #default="{ row }">
            <el-tag :type="addOutcomeType(row.outcome)" size="small">{{ addOutcomeLabel(row.outcome) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="140">
          <template #default="{ row }">{{ row.outcome === 'error' ? row.message ?? row.code : '' }}</template>
        </el-table-column>
      </el-table>
      <div class="op-meta muted">
        {{ fmtTime(op.createdAt) }} · 请求编号 {{ shortId(op.requestId) }}<span v-if="op.commandId"> · 命令 {{ shortId(op.commandId) }}</span>
      </div>
      <el-button
        v-if="op.state === 'unknown' && !op.commandId"
        size="small"
        type="warning"
        :disabled="!canControl"
        @click="ops.retry(op)"
      >
        用同一请求编号重试
      </el-button>
    </el-card>
  </el-drawer>
</template>
