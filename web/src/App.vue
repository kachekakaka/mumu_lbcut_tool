<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { fmtTime } from './format'
import { isActiveOperation } from './commands'
import { useMobile } from './mobile'
import { canControl, connection, pageError, startPolling, stopPolling } from './poller'
import { ops } from './operations'
import OperationsDrawer from './components/OperationsDrawer.vue'

const route = useRoute()
const mobile = useMobile()
const opsOpen = ref(false)
const activeOps = computed(() => ops.list.filter(isActiveOperation).length)
const pendingRetry = computed(() => ops.list.filter((op) => op.state === 'unknown' && !op.commandId).length)

const NAV = [
  { path: '/', label: '总览' },
  { path: '/instances', label: '实例' },
  { path: '/targets', label: '主播' },
  { path: '/jobs', label: '任务' },
  { path: '/segments', label: '切片' },
  { path: '/accounts', label: '上传平台' },
]

const serviceWarning = computed(() => {
  const s = connection.system
  if (connection.ok !== true || !s) return ''
  if (s.service_state === 'closing') return '服务正在关闭，操作已禁用'
  if (s.service_state === 'failed') return '调度失败，操作已禁用'
  if (s.service_state !== 'running') return `服务状态：${s.service_state}，操作已禁用`
  if (!s.scheduler_running) return '调度未运行，操作已禁用'
  if (s.last_tick_at !== null && s.server_time - s.last_tick_at > 15) {
    return `调度循环 ${Math.round(s.server_time - s.last_tick_at)} 秒未更新`
  }
  return ''
})

onMounted(startPolling)
onUnmounted(() => {
  ops.dispose()
  stopPolling()
})
</script>

<template>
  <el-container class="layout">
    <el-header class="topbar" height="auto">
      <div class="brand">录播调度控制台</div>
      <div class="status">
        <el-tag v-if="connection.ok === null" type="info" effect="dark">连接中</el-tag>
        <el-tag v-else-if="connection.ok" type="success" effect="dark">连接正常</el-tag>
        <el-tag v-else type="danger" effect="dark">连接断开</el-tag>
        <span class="muted">
          {{ connection.ok === false ? '最后成功更新' : '更新于' }} {{ fmtTime(connection.lastSuccessAt) }}
        </span>
        <el-button size="small" @click="opsOpen = true">
          操作记录<span v-if="activeOps">（{{ activeOps }} 进行中）</span><span v-if="pendingRetry" class="warn-text">（{{ pendingRetry }} 待重试）</span>
        </el-button>
      </div>
    </el-header>
    <el-alert
      v-if="connection.ok === false"
      type="error"
      :closable="false"
      show-icon
      :title="`无法连接调度服务，页面保留最后一次数据（${fmtTime(connection.lastSuccessAt)}），操作已禁用；恢复后自动刷新`"
    />
    <el-alert v-else-if="serviceWarning" type="warning" :closable="false" show-icon :title="serviceWarning" />
    <el-container>
      <el-aside v-if="!mobile" width="120px">
        <el-menu router :default-active="route.path">
          <el-menu-item v-for="n in NAV" :key="n.path" :index="n.path">{{ n.label }}</el-menu-item>
        </el-menu>
      </el-aside>
      <el-main>
        <el-menu v-if="mobile" router mode="horizontal" :default-active="route.path" :ellipsis="false" class="mobile-nav">
          <el-menu-item v-for="n in NAV" :key="n.path" :index="n.path">{{ n.label }}</el-menu-item>
        </el-menu>
        <el-alert v-if="pageError" type="error" :closable="false" show-icon :title="`数据读取失败：${pageError}`" />
        <el-alert v-if="!canControl && connection.ok === true && !serviceWarning" type="warning" :closable="false" title="服务状态未知，操作已禁用" />
        <router-view />
      </el-main>
    </el-container>
    <OperationsDrawer v-model="opsOpen" />
  </el-container>
</template>
