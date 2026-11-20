<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onLocalConsole, PLATFORM_ORIGIN, request } from '../api/segmentUploads'

const props = defineProps<{ page: 'accounts' | 'works' | 'tasks'; taskId?: string }>()
const emit = defineEmits<{ accountUpdated: [id: string]; taskUpdated: [id: string] }>()
const frame = ref<HTMLIFrameElement>()
const loading = ref(false)
const available = ref(false)
const error = ref('')
const local = onLocalConsole()
const validId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/
const protocol = 'upload-platform-embed'
let generation = 0
let ready = false
let deadline: ReturnType<typeof setTimeout> | undefined
const url = computed(() => PLATFORM_ORIGIN + '/embed/' + props.page + (props.taskId ? '/' + props.taskId : ''))

async function open() {
  clearTimeout(deadline)
  const current = ++generation
  available.value = false
  ready = false
  error.value = ''
  if (!local) return
  if (props.taskId && !validId.test(props.taskId)) {
    error.value = '任务编号无效'
    return
  }
  loading.value = true
  try {
    const query = new URLSearchParams({ page: props.page })
    if (props.taskId) query.set('task_id', props.taskId)
    await request('/panel?' + query)
    if (current !== generation) return
    available.value = true
    deadline = setTimeout(() => {
      if (!ready && current === generation) {
        loading.value = false
        available.value = false
        error.value = '上传平台页面没有响应，请检查本机服务后重试'
      }
    }, 8000)
  } catch (cause) {
    if (current === generation) {
      loading.value = false
      error.value = cause instanceof Error ? cause.message : '上传平台暂不可用'
    }
  }
}

function receive(event: MessageEvent) {
  if (!available.value || !frame.value || event.origin !== PLATFORM_ORIGIN || event.source !== frame.value.contentWindow) return
  const data = event.data
  if (!data || data.protocol !== protocol || data.version !== 1) return
  const payload = data.payload
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return
  if (data.type === 'ready' && Object.keys(payload).length === 0) {
    ready = true
    clearTimeout(deadline)
    loading.value = false
    if (props.taskId) frame.value.contentWindow?.postMessage({
      protocol, version: 1, type: 'open-task', payload: { task_id: props.taskId },
    }, PLATFORM_ORIGIN)
  }
  if (!ready || !validId.test(typeof payload.id === 'string' ? payload.id : '')
    || typeof payload.status !== 'string' || !/^[A-Za-z0-9_]{1,48}$/.test(payload.status)
    || Object.keys(payload).length !== 2) return
  if (data.type === 'account-updated' && props.page === 'accounts') emit('accountUpdated', payload.id)
  if (data.type === 'task-updated' && props.page === 'tasks' && (!props.taskId || payload.id === props.taskId)) {
    emit('taskUpdated', payload.id)
  }
}

watch(() => [props.page, props.taskId], () => void open())
onMounted(() => {
  window.addEventListener('message', receive)
  void open()
})
onUnmounted(() => {
  window.removeEventListener('message', receive)
  generation++
  clearTimeout(deadline)
})
</script>

<template>
  <el-alert v-if="!local" title="请在录制机打开 http://127.0.0.1:8000 使用上传平台" type="info" :closable="false" />
  <div v-else>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
    <el-button v-if="error" class="reload" @click="open">重新连接</el-button>
    <p v-if="loading" role="status">正在连接上传平台…</p>
    <iframe v-if="available" ref="frame" :src="url" title="上传平台" class="platform-frame" referrerpolicy="no-referrer" />
  </div>
</template>

<style scoped>
.reload { margin-top: 12px; }
.platform-frame { width: 100%; height: calc(100vh - 160px); min-height: 400px; border: 0; }
</style>
