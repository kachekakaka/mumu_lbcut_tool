<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { request } from '../api/segmentUploads'
import type { UploadAccount } from '../api/segmentUploads'
import { fmtTime } from '../format'
import UploadPanel from './UploadPanel.vue'

interface Settings {
  enabled: boolean
  saved: { deployment: string; category_id: number; account: UploadAccount } | null
  deployment: string | null
  accounts: UploadAccount[]
  problem?: string
}
interface Session {
  session_key: string
  started_at: number
  title: string | null
  status: string
  problem: string | null
  task_id: string | null
  observed?: string
}

const model = defineModel<boolean>({ required: true })
const props = defineProps<{ targetId: string; nickname: string }>()
const settings = ref<Settings>()
const sessions = ref<Session[]>([])
const enabled = ref(false)
const account = ref('')
const category = ref<number>()
const loading = ref(false)
const saving = ref(false)
const accountsOpen = ref(false)
const error = ref('')
const statusError = ref('')
let generation = 0
let timer: ReturnType<typeof setInterval> | undefined
let statusGeneration = -1
const path = computed(() => '/session-uploads/' + props.targetId)
const statuses: Record<string, string> = {
  recording: '录制中', waiting_close: '等待本场结束', waiting_archive: '等待录像归档',
  checking: '正在核对切片', only_record: '只录',
  pending: '待交付', review: '待核对', delivered: '平台已受理',
}

function recoverMessage(cause: unknown) {
  return cause instanceof Error ? cause.message : '本机投稿服务未响应'
}

async function loadSessions() {
  const current = generation
  if (statusGeneration === current) return
  statusGeneration = current
  try {
    const { items } = await request<{items: Session[]}>(path.value)
    if (current !== generation) return
    sessions.value = items
    statusError.value = ''
  } catch (cause) {
    if (current === generation) statusError.value = recoverMessage(cause)
  } finally {
    if (statusGeneration === current) statusGeneration = -1
  }
}

async function open() {
  const current = ++generation
  settings.value = undefined
  sessions.value = []
  error.value = statusError.value = ''
  loading.value = true
  void loadSessions()
  try {
    const data = await request<Settings>(path.value + '/settings')
    if (current !== generation) return
    settings.value = data
    enabled.value = data.enabled
    account.value = data.saved?.deployment === data.deployment ? data.saved?.account.id ?? '' : ''
    category.value = data.saved?.category_id
  } catch (cause) {
    if (current === generation) error.value = recoverMessage(cause)
  } finally {
    if (current === generation) loading.value = false
  }
}

async function save() {
  if (saving.value || !settings.value) return
  const current = generation
  const next = enabled.value
  const selected = settings.value.accounts.find(item => item.id === account.value)
  if (next && (!selected || !category.value)) return
  saving.value = true
  error.value = ''
  try {
    await request(path.value + '/settings', next ? {
      enabled: true, deployment: settings.value.deployment, account_id: account.value, category_id: category.value,
      remote_user_id: selected?.remote_user_id,
    } : { enabled: false })
    ElMessage.success(next ? '已保存自动投稿' : '已关闭自动投稿')
    if (current === generation) await open()
  } catch (cause) {
    if (current === generation) error.value = recoverMessage(cause)
  } finally {
    saving.value = false
  }
}

async function refreshAccounts() {
  const current = generation
  try {
    const data = await request<Settings>(path.value + '/settings')
    if (current === generation) settings.value = data
  } catch (cause) {
    if (current === generation) error.value = recoverMessage(cause)
  }
}

watch(() => [model.value, props.targetId], () => {
  clearInterval(timer)
  if (model.value) {
    void open()
    timer = setInterval(() => void loadSessions(), 4000)
  } else {
    generation++
  }
}, { immediate: true })
onUnmounted(() => { clearInterval(timer); generation++ })
</script>

<template>
  <el-dialog v-model="model" :title="'自动投稿 · ' + nickname" width="min(720px, 96vw)">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-loading="loading">
      <template v-if="settings">
        <el-form label-position="top" @submit.prevent="save">
          <el-form-item label="自动投稿">
            <el-switch v-model="enabled" :disabled="saving" aria-label="自动投稿" />
          </el-form-item>
          <el-form-item v-if="settings.saved" label="已保存账号">
            {{ settings.saved.account.display_name }}（MID {{ settings.saved.account.remote_user_id }}）
          </el-form-item>
          <el-form-item v-if="enabled && settings.deployment" label="投稿账号">
            <el-select v-model="account" :disabled="saving" placeholder="选择当前部署的可用账号">
              <el-option v-for="item in settings.accounts" :key="item.id" :value="item.id"
                :label="item.display_name + '（MID ' + item.remote_user_id + '）'" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="enabled && settings.deployment" label="B 站分区 ID">
            <el-input-number v-model="category" :disabled="saving" :min="1" :max="65535" :precision="0" />
          </el-form-item>
          <p class="muted">录完后按场次投稿，发布范围为仅自己可见。关闭后，本场尚未提交的自动安排会停止，录制继续。</p>
          <p v-if="settings.problem" class="muted">{{ settings.problem }}</p>
          <el-button @click="accountsOpen = true">管理投稿账号</el-button>
          <el-button native-type="submit" type="primary" :loading="saving"
            :disabled="enabled && (!settings.deployment || !category || !settings.accounts.some(a => a.id === account))">
            保存
          </el-button>
        </el-form>
      </template>
    </div>
    <h3>最近场次</h3>
    <el-alert v-if="statusError" :title="statusError" type="warning" :closable="false" />
    <el-table :data="sessions" size="small" empty-text="暂无场次记录">
      <el-table-column label="开录时间" min-width="135">
        <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column prop="title" label="直播间标题" min-width="135" show-overflow-tooltip />
      <el-table-column label="交付结果" min-width="210">
        <template #default="{ row }">
          <div>{{ statuses[row.status] || row.status }}<template v-if="row.observed === 'UNCERTAIN'"> · 平台待核验</template></div>
          <router-link v-if="row.task_id" :to="'/uploads/tasks/' + row.task_id">查看任务</router-link>
          <div v-if="row.problem" class="muted">{{ row.problem }}</div>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog v-model="accountsOpen" title="上传平台账号" width="min(960px, 96vw)" append-to-body destroy-on-close>
      <UploadPanel v-if="accountsOpen" page="accounts" @account-updated="refreshAccounts" />
    </el-dialog>
  </el-dialog>
</template>
