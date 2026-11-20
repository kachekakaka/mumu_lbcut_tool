<script setup lang="ts">
import { computed, ref } from 'vue'
import { arrangementStatus, problemText, segmentUploads } from '../api/segmentUploads'
import type { Arrangement, ArrangementDetail, PostingFields, UploadContext } from '../api/segmentUploads'
import type { Segment } from '../api/types'
import { fmtDuration, fmtTime } from '../format'
import QualityFlags from './QualityFlags.vue'

const emit = defineEmits<{ changed: [] }>()
const visible = ref(false)
const busy = ref(false)
const error = ref('')
const selected = ref<Segment>()
const detail = ref<ArrangementDetail>()
const context = ref<UploadContext>()
const account = ref('')
const title = ref('')
const description = ref('')
const tags = ref('')
const category = ref<number>()
const copyright = ref<1 | 2>(1)
const source = ref('')
const mode = ref<PostingFields['publish_mode']>()
const quality = ref(false)
const key = ref('')
const revision = ref(false)
let operation = 0

const canConfirm = computed(() => !!(
  context.value && account.value && title.value.trim() && tags.value.trim() && category.value && mode.value &&
  (copyright.value === 1 || source.value.trim()) && (!context.value.quality_flags.length || quality.value)
))

async function run<T>(fn: () => Promise<T>, finish: (value: T) => void) {
  const active = ++operation
  error.value = ''
  busy.value = true
  try {
    const value = await fn()
    if (active === operation && visible.value) finish(value)
  } catch (e) {
    if (active === operation && visible.value) error.value = e instanceof Error ? e.message : '操作失败，请重新查看原切片'
  } finally {
    if (active === operation) busy.value = false
  }
}

function show(row: Segment, existing?: Arrangement) {
  selected.value = row
  visible.value = true
  context.value = detail.value = undefined
  if (existing) void run(() => segmentUploads.detail(existing.id), value => { detail.value = value })
  else void openForm()
}

async function openForm(revise?: string) {
  if (selected.value?.status !== 'finished') return
  const row = selected.value
  context.value = undefined
  revision.value = !!revise
  await run(() => segmentUploads.context(row, revise), value => {
    context.value = value
    detail.value = undefined
    key.value = crypto.randomUUID()
    account.value = value.accounts.length === 1 ? value.accounts[0]!.id : ''
    title.value = value.defaults.title ?? ''
    description.value = value.defaults.description ?? ''
    tags.value = value.defaults.tags?.join('，') ?? ''
    category.value = value.defaults.category_id
    copyright.value = value.defaults.copyright ?? 1
    source.value = value.defaults.source ?? ''
    mode.value = value.defaults.publish_mode && value.publish_modes.includes(value.defaults.publish_mode)
      ? value.defaults.publish_mode : value.publish_modes.length === 1 ? value.publish_modes[0] : undefined
    quality.value = false
  })
}

async function confirm() {
  if (busy.value || !context.value || !canConfirm.value) return
  const ctx = context.value
  await run(() => segmentUploads.confirm(key.value, ctx, account.value, {
    title: title.value, description: description.value,
    tags: tags.value.split(/[,，]/).map(t => t.trim()).filter(Boolean),
    category_id: category.value!, copyright: copyright.value, source: copyright.value === 2 ? source.value : '',
    publish_mode: mode.value!,
  }, quality.value), value => {
    context.value = undefined
    detail.value = value
  })
  emit('changed')
}

async function reload(resume = false) {
  const current = detail.value
  if (!current || busy.value) return
  await run(() => resume ? segmentUploads.resume(current) : segmentUploads.detail(current.id), value => { detail.value = value })
  emit('changed')
}

defineExpose({ show })
</script>

<template>
  <el-dialog v-model="visible" :title="context ? (revision ? '修订原安排' : '安排上传') : '切片上传安排'" width="min(650px, 95vw)">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-loading="busy" class="segment-upload-dialog">
      <template v-if="context">
        <p><strong>{{ context.nickname }} · 第 {{ selected?.segment_index }} 片</strong></p>
        <p class="upload-file">{{ context.filename }}（{{ fmtDuration(context.duration_sec) }}）</p>
        <p>录制任务 {{ selected?.job_id }}</p>
        <p>目标部署 {{ context.deployment }}（端口 {{ context.port }}）</p>
        <el-alert v-if="!context.accounts.length" title="这个部署当前没有可安排的账号" type="warning" :closable="false" />
        <el-form label-width="88px" label-position="left">
          <el-form-item label="B 站账号">
            <el-select v-model="account" placeholder="选择投稿账号" aria-label="B 站账号">
              <el-option v-for="a in context.accounts" :key="a.id" :value="a.id" :label="`${a.display_name}（MID ${a.remote_user_id ?? '未知'}）`" />
            </el-select>
          </el-form-item>
          <el-form-item label="标题"><el-input v-model="title" :maxlength="context.limits.title" aria-label="投稿标题" /></el-form-item>
          <el-form-item label="标签"><el-input v-model="tags" placeholder="用逗号分隔" aria-label="投稿标签" /></el-form-item>
          <el-form-item label="分区编号"><el-input-number v-model="category" :min="1" :max="65535" :step-strictly="true" aria-label="分区编号" /></el-form-item>
          <el-form-item label="版权">
            <el-radio-group v-model="copyright"><el-radio :value="1">自制</el-radio><el-radio :value="2">转载</el-radio></el-radio-group>
          </el-form-item>
          <el-form-item v-if="copyright === 2" label="转载来源"><el-input v-model="source" aria-label="转载来源" /></el-form-item>
          <el-form-item label="发布范围">
            <el-radio-group v-model="mode">
              <el-radio v-for="m in context.publish_modes" :key="m" :value="m">{{ m === 'public' ? '公开' : '仅自己可见' }}</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="简介"><el-input v-model="description" type="textarea" :maxlength="context.limits.description" aria-label="投稿简介" /></el-form-item>
          <el-form-item v-if="context.quality_flags.length" label="质量提示">
            <QualityFlags :flags="context.quality_flags" />
            <el-checkbox v-model="quality">已核对，仍安排上传</el-checkbox>
          </el-form-item>
        </el-form>
        <p>{{ context.worker_online ? '确认后 Worker 可能立即执行。' : '当前没有在线 Worker，Core 接单后会等待 Worker。' }}</p>
      </template>
      <template v-else-if="detail">
        <p><strong>{{ arrangementStatus(detail) }}</strong></p>
        <p class="upload-file">{{ detail.filename }} · {{ detail.parameters.title }}</p>
        <p>{{ detail.account.display_name }}（MID {{ detail.account.remote_user_id ?? '未知' }}） · {{ detail.parameters.publish_mode === 'public' ? '公开' : '仅自己可见' }}</p>
        <dl>
          <dt>目标部署</dt><dd>{{ detail.deployment }}</dd>
          <dt>安排编号</dt><dd>{{ detail.id }}</dd>
          <dt>Core 作品</dt><dd>{{ detail.work_id }}</dd>
          <dt>Core 任务</dt><dd>{{ detail.task_id || '等待关联' }}</dd>
          <dt>最近成功查询</dt><dd>{{ fmtTime(detail.observed_at) }}</dd>
          <template v-if="detail.bvid || detail.aid"><dt>稿件编号</dt><dd>{{ [detail.bvid, detail.aid].filter(Boolean).join(' / ') }}</dd></template>
          <template v-if="detail.reason"><dt>Core 原因</dt><dd>{{ detail.reason }}</dd></template>
        </dl>
        <el-alert v-if="detail.problem" :title="problemText(detail.problem)" type="warning" :closable="false" />
      </template>
      <p v-else-if="busy">正在读取切片和部署信息…</p>
    </div>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button v-if="context" type="primary" :disabled="!canConfirm" :loading="busy" @click="confirm">{{ revision ? '确认修订并安排' : '确认安排' }}</el-button>
      <template v-else-if="detail">
        <el-button :disabled="busy" @click="reload()">查询原单</el-button>
        <el-button v-if="detail.can_continue && selected?.status === 'finished'" type="primary" :disabled="busy" @click="reload(true)">继续原安排</el-button>
        <el-button v-if="detail.can_revise && selected?.status === 'finished'" :disabled="busy" @click="openForm(detail.id)">修订原安排</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<style scoped>
.segment-upload-dialog { min-height: 42px; overflow-wrap: anywhere; }
.segment-upload-dialog p { margin: 0.65rem 0; }
.segment-upload-dialog dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; }
.segment-upload-dialog dd { margin: 0; }
.upload-file { font-weight: 500; }
</style>
