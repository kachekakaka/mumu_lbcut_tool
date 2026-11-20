<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { onLocalConsole, request } from '../api/segmentUploads'
import UploadPanel from '../components/UploadPanel.vue'

const route = useRoute()
const page = computed(() => route.meta.uploadPage as 'accounts' | 'works' | 'tasks')
const taskId = computed(() => typeof route.params.taskId === 'string' ? route.params.taskId : undefined)

function refreshTask(id: string) {
  void request('/tasks/' + id).catch(() => undefined)
}
</script>

<template>
  <div v-if="onLocalConsole()" class="toolbar">
    <router-link to="/accounts">账号</router-link>
    <router-link to="/uploads/works">作品</router-link>
    <router-link to="/uploads/tasks">上传任务</router-link>
  </div>
  <UploadPanel :page="page" :task-id="taskId" @task-updated="refreshTask" />
</template>
