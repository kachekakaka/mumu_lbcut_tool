<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import type { Target } from '../api/types'
import { fmtClock, fmtTime } from '../format'
import { jobStatusLabel, jobStatusType, reasonLabel, TRUSTED_STATUS, trustedStatusLabel, trustedStatusType } from '../labels'
import { boolParam, PAGE_SIZES, usePager } from '../pager'
import { canControl, connection, refreshNow, usePageLoader } from '../poller'
import { ops } from '../operations'
import { useMobile } from '../mobile'
import AddTargetsDialog from '../components/AddTargetsDialog.vue'
import SessionPostingDialog from '../components/SessionPostingDialog.vue'
import { onLocalConsole } from '../api/segmentUploads'

const route = useRoute()
const mobile = useMobile()
const router = useRouter()
const filters = reactive({
  q: '',
  trusted_status: '',
  auto_record: '',
  has_notice: route.query.notice === 'true' ? 'true' : '',
})
const focus = computed(() => String(route.query.focus ?? ''))
const items = ref<Target[]>([])
const addOpen = ref(false)
const postingOpen = ref(false)
const postingTarget = ref<Target>()
const callbackOwner = Symbol('targets')
onUnmounted(() => ops.detachOwner(callbackOwner))
const { page, size, total, query, fit } = usePager()

usePageLoader(async () => {
  const p = await api.targets({
    ...query(),
    q: filters.q.trim() || undefined,
    trusted_status: filters.trusted_status || undefined,
    auto_record: boolParam(filters.auto_record),
    has_notice: boolParam(filters.has_notice),
  })
  items.value = p.items
  fit(p)
})
watch(filters, () => {
  page.value = 1
  void refreshNow()
}, { deep: true })

const name = (t: Target) => t.nickname || t.raw
const rowClass = ({ row }: { row: Target }) => (focus.value && row.id === focus.value ? 'focus-row' : '')

function notices(t: Target): { type: 'warning' | 'danger' | 'info'; text: string }[] {
  const list: { type: 'warning' | 'danger' | 'info'; text: string }[] = []
  if (t.suppressed) list.push({ type: 'info', text: '本场已抑制，不再自动开录' })
  if (t.needs_fix_reason) list.push({ type: 'danger', text: `需要修正：${reasonLabel(t.needs_fix_reason)}` })
  if (t.is_retrying) {
    const now = connection.system?.server_time ?? Date.now() / 1000
    const next = t.retry_at > now ? `下次 ${fmtClock(t.retry_at)}` : '等待重试'
    const reason = t.last_failure_reason ? `；${reasonLabel(t.last_failure_reason)}` : ''
    list.push({ type: 'warning', text: `失败重试中：第 ${t.attempts} 次，${next}${reason}` })
  }
  return list
}

function record(t: Target) {
  const targetId = t.id
  ops.start('record', targetId, `立即开录：${name(t)}`, (key) => api.record(targetId, key), (op) => {
    const result = op.command?.result
    if (op.state === 'succeeded' && result?.outcome === 'existing' && result.job_id && route.path === '/targets') {
      void router.push({ path: '/jobs', query: { job: String(result.job_id) } })
    }
  }, callbackOwner)
  ElMessage.info('已提交开录，后端会重新确认开播')
}

function toggleAuto(t: Target) {
  const next = !t.auto_record
  ops.start('update', t.id, `${next ? '开启' : '关闭'}自动录制：${name(t)}`, (key) => api.setAutoRecord(t.id, next, key))
  if (!next) ElMessage.info('关闭自动录制不会停止已承接的本场；如需结束本场，请用“停止本场”或任务页平滑停止')
  return false
}

async function stopSession(t: Target) {
  // 固定使用点击时看到的场次；确认期间换场由后端拒绝，不自动换成新场次重发。
  const session = t.session_key
  if (!session) return
  try {
    await ElMessageBox.confirm(
      `确认停止「${name(t)}」本场？本场不再续录，正在录制的可控任务会平滑停止；继续监控，自动录制开关不变。`,
      '停止本场',
      { type: 'warning', confirmButtonText: '停止本场', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  ops.start('stop_target', t.id, `停止本场：${name(t)}`, (key) => api.stopTarget(t.id, session, key))
}

function openPosting(t: Target) {
  postingTarget.value = t
  postingOpen.value = true
}

async function remove(t: Target) {
  try {
    await ElMessageBox.confirm(
      `确认删除主播「${name(t)}」？正在录制的任务会平滑停止，录像保留。`,
      '删除主播',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  ops.start('remove', t.id, `删除主播：${name(t)}`, (key) => api.removeTarget(t.id, key))
}
</script>

<template>
  <div>
    <div class="toolbar">
      <el-input v-model="filters.q" placeholder="搜索昵称或地址" clearable size="small" style="width: 180px" />
      <el-select v-model="filters.trusted_status" placeholder="开播状态" clearable size="small" style="width: 110px">
        <el-option v-for="(label, key) in TRUSTED_STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.auto_record" placeholder="自动录制" clearable size="small" style="width: 110px">
        <el-option label="自动录制开" value="true" />
        <el-option label="自动录制关" value="false" />
      </el-select>
      <el-select v-model="filters.has_notice" placeholder="提示" clearable size="small" style="width: 100px">
        <el-option label="有提示" value="true" />
        <el-option label="无提示" value="false" />
      </el-select>
      <el-button type="primary" size="small" :disabled="!canControl" @click="addOpen = true">新增主播</el-button>
    </div>
    <el-table :data="items" :row-class-name="rowClass" row-key="id" size="small" stripe empty-text="暂无主播">
      <el-table-column label="昵称" min-width="120">
        <template #default="{ row }">{{ name(row) }}</template>
      </el-table-column>
      <el-table-column prop="raw" label="地址" min-width="160" show-overflow-tooltip />
      <el-table-column label="开播状态" min-width="130">
        <template #default="{ row }">
          <el-tag :type="trustedStatusType(row.trusted_status)" size="small">{{ trustedStatusLabel(row.trusted_status) }}</el-tag>
          <div v-if="row.confirmed_at" class="muted">确认于 {{ fmtTime(row.confirmed_at) }}</div>
          <div v-else-if="row.observed_at" class="muted">最近查询 {{ fmtTime(row.observed_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="自动录制" width="90">
        <template #default="{ row }">
          <el-switch
            :model-value="row.auto_record"
            :loading="ops.busy(row.id, 'update')"
            :disabled="!canControl"
            :before-change="() => toggleAuto(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="当前任务" min-width="110">
        <template #default="{ row }">
          <router-link v-if="row.active_job_id" :to="{ path: '/jobs', query: { job: row.active_job_id } }">
            <el-tag :type="jobStatusType(row.active_job_status)" size="small">{{ jobStatusLabel(row.active_job_status) }}</el-tag>
          </router-link>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="提示" min-width="220">
        <template #default="{ row }">
          <div v-for="(n, i) in notices(row)" :key="i"><el-tag :type="n.type" size="small" class="wrap-tag">{{ n.text }}</el-tag></div>
          <span v-if="!notices(row).length">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" :min-width="onLocalConsole() ? 290 : 220" :fixed="mobile ? false : 'right'">
        <template #default="{ row }">
          <el-tooltip content="后端会重新确认开播；已抑制或需要修正的主播同样可用" placement="top">
            <el-button size="small" :disabled="!canControl" :loading="ops.busy(row.id, 'record')" @click="record(row)">立即开录</el-button>
          </el-tooltip>
          <el-button
            size="small"
            type="warning"
            :disabled="!canControl || !row.recording_intent || !row.session_key"
            :loading="ops.busy(row.id, 'stop_target')"
            @click="stopSession(row)"
          >
            停止本场
          </el-button>
          <el-button v-if="onLocalConsole()" size="small" @click="openPosting(row)">自动投稿</el-button>
          <el-button size="small" type="danger" :disabled="!canControl" :loading="ops.busy(row.id, 'remove')" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="PAGE_SIZES" layout="total, sizes, prev, pager, next" small />
    <p class="muted">“停止本场”在本场录制意图有效时可用（包括失败重试中、等待对账），确认后固定停止点击时看到的场次。</p>
    <AddTargetsDialog v-model="addOpen" />
    <SessionPostingDialog v-if="postingTarget" v-model="postingOpen" :target-id="postingTarget.id" :nickname="name(postingTarget)" />
  </div>
</template>
