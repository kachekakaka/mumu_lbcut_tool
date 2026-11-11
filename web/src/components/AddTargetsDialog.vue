<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import type { Operation } from '../commands'
import { describeOperation, isActiveOperation } from '../commands'
import { addOutcomeLabel, addOutcomeType, commandStatusLabel, commandStatusType } from '../labels'
import { useMobile } from '../mobile'
import { canControl } from '../poller'
import { ops } from '../operations'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const mobile = useMobile()

const text = ref('')
const manualOnly = ref(false)
const op = ref<Operation | null>(null)

const lines = computed(() => text.value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean))
const tooLong = computed(() => lines.value.some((l) => l.length > 2048))
const busy = computed(() => !!op.value && isActiveOperation(op.value))
const levelType = { success: 'success', info: 'info', warning: 'warning', error: 'error' } as const

function submit() {
  const targets = [...lines.value]
  const manual = manualOnly.value
  op.value = ops.start('add', '', `新增主播（${targets.length} 条）`, (key) => api.addTargets(targets, manual, key))
}

function reset() {
  text.value = ''
  manualOnly.value = false
  op.value = null
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="新增主播"
    :fullscreen="mobile"
    width="560px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="!op">
      <el-input
        v-model="text"
        type="textarea"
        :rows="6"
        placeholder="每行一个直播间地址或抖音号"
      />
      <div class="form-row">
        <el-checkbox v-model="manualOnly">仅手动录制（不自动开录）</el-checkbox>
      </div>
      <p class="muted">已存在的主播不会因重复添加而改变设置。一次最多 100 条，每条不超过 2048 个字符。</p>
      <el-alert v-if="lines.length > 100" type="error" title="一次最多 100 条" :closable="false" />
      <el-alert v-if="tooLong" type="error" title="有地址超过 2048 个字符" :closable="false" />
    </template>
    <template v-else>
      <div class="op-head">
        <strong>{{ op.title }}</strong>
        <el-tag :type="commandStatusType(op.state)" size="small">{{ commandStatusLabel(op.state) }}</el-tag>
      </div>
      <el-alert :type="levelType[describeOperation(op).level]" :title="describeOperation(op).text" :closable="false" show-icon>
        <div v-for="(n, i) in describeOperation(op).notes" :key="i">{{ n }}</div>
      </el-alert>
      <el-table v-if="Array.isArray(op.command?.result?.items)" :data="op.command!.result!.items" size="small">
        <el-table-column prop="input" label="输入" min-width="180" show-overflow-tooltip />
        <el-table-column label="结果" min-width="120">
          <template #default="{ row }">
            <el-tag :type="addOutcomeType(row.outcome)" size="small">{{ addOutcomeLabel(row.outcome) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="140">
          <template #default="{ row }">{{ row.outcome === 'error' ? row.message ?? row.code : '' }}</template>
        </el-table-column>
      </el-table>
      <el-button v-if="op.state === 'unknown' && !op.commandId" type="warning" :disabled="!canControl" @click="ops.retry(op)">
        用同一请求编号重试
      </el-button>
    </template>
    <template #footer>
      <template v-if="!op">
        <el-button @click="emit('update:modelValue', false)">取消</el-button>
        <el-button
          type="primary"
          :disabled="!canControl || !lines.length || lines.length > 100 || tooLong"
          @click="submit"
        >
          提交
        </el-button>
      </template>
      <template v-else>
        <el-button :disabled="busy" @click="reset">继续添加</el-button>
        <el-button type="primary" @click="emit('update:modelValue', false)">关闭</el-button>
      </template>
    </template>
  </el-dialog>
</template>
