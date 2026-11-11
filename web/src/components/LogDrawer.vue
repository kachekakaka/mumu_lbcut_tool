<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api'
import type { JobLog } from '../api/types'
import { shortId } from '../format'
import { useMobile } from '../mobile'
import { errorText } from '../poller'

const props = defineProps<{ modelValue: boolean; jobId: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const mobile = useMobile()
const lines = ref(200)
const log = ref<JobLog | null>(null)
const error = ref('')
const loading = ref(false)

async function load() {
  if (!props.jobId || loading.value) return
  loading.value = true
  try {
    log.value = await api.jobLog(props.jobId, lines.value)
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    loading.value = false
  }
}

watch(() => [props.modelValue, props.jobId], ([open]) => {
  if (open) {
    log.value = null
    error.value = ''
    void load()
  }
})
watch(lines, () => void load())
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    :title="`任务日志 ${shortId(jobId)}`"
    :size="mobile ? '100%' : '60%'"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="toolbar">
      <span>最后</span>
      <el-select v-model="lines" size="small" style="width: 100px">
        <el-option v-for="n in [200, 500, 1000, 2000]" :key="n" :label="`${n} 行`" :value="n" />
      </el-select>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <span class="muted">日志只手动刷新</span>
    </div>
    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon />
    <template v-if="log">
      <el-alert v-if="!log.available" type="info" :title="`日志不可用：${log.reason ?? '未知原因'}`" :closable="false" show-icon />
      <template v-else>
        <p class="muted">
          返回 {{ log.returned_lines }} 行<span v-if="log.truncated">；内容已截断，只显示末尾部分</span>
        </p>
        <pre class="log-text">{{ log.text }}</pre>
      </template>
    </template>
  </el-drawer>
</template>
