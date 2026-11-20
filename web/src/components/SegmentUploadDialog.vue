<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { Segment } from '../api/types'
import { arrangementStatus, segmentUploads } from '../api/segmentUploads'
import type { Arrangement, Parameters, Preview, UploadContext } from '../api/segmentUploads'
import { qualityFlagLabel } from '../labels'

const emit = defineEmits<{ changed: [] }>()
const router = useRouter()
const open = ref(false)
const busy = ref(false)
const error = ref('')
const segment = ref<Segment>()
const context = ref<UploadContext>()
const detail = ref<Arrangement>()
const preview = ref<Preview>()
const params = ref<Parameters>()
const accountId = ref('')
const tags = ref('')
const quality = ref(false)
const revise = ref<string>()
const selected = computed(() => context.value?.accounts.find(a => a.id === accountId.value))
let generation = 0

async function run(action: (current: number) => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  const current = generation
  try {
    await action(current)
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : '本机投稿服务暂不可用'
  } finally {
    if (current === generation) busy.value = false
  }
}

function show(row: Segment, existing?: Arrangement, edit?: string) {
  if (existing?.task_id) {
    void router.push('/uploads/tasks/' + existing.task_id)
    return
  }
  generation++
  open.value = true
  busy.value = false
  segment.value = row
  detail.value = existing
  context.value = preview.value = undefined
  revise.value = edit
  quality.value = false
  void run(async current => {
    if (existing) {
      const record = await segmentUploads.detail(existing.id)
      if (current === generation) handleRecord(record)
    } else {
      const data = await segmentUploads.context(row, edit)
      if (current !== generation) return
      context.value = data
      params.value = { ...data.defaults }
      accountId.value = data.account_id ?? (data.accounts.length === 1 ? data.accounts[0]!.id : '')
      tags.value = data.defaults.tags.join('，')
    }
  })
}

function handleRecord(record: Arrangement) {
  detail.value = record
  preview.value = undefined
  emit('changed')
  if (record.task_id) {
    open.value = false
    void router.push('/uploads/tasks/' + record.task_id)
  }
}

function makePreview() {
  const row = segment.value
  const form = params.value
  const account = selected.value
  const deployment = context.value?.deployment
  if (!row || !form || !account || !deployment) return
  void run(async current => {
    const data = await segmentUploads.preview(row, deployment, account, {
      ...form, source: form.copyright === 1 ? '' : form.source,
      tags: tags.value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
    }, revise.value)
    if (current === generation) {
      preview.value = data
      quality.value = false
    }
  })
}

function confirm() {
  const ticket = preview.value?.ticket
  if (!ticket) return
  void run(async current => {
    const result = await segmentUploads.confirm(ticket, quality.value)
    if (current === generation) handleRecord(result)
    else emit('changed')
  })
}

function refresh(resume = false) {
  if (!detail.value) return
  const id = detail.value.id
  void run(async current => {
    const record = await (resume ? segmentUploads.resume(id) : segmentUploads.detail(id))
    if (current === generation) handleRecord(record)
  })
}

function cancel() {
  generation++
  busy.value = false
  open.value = false
}

defineExpose({ show })
</script>

<template>
  <el-dialog v-model="open" title="安排上传" width="min(680px, 96vw)" @close="cancel">
    <div v-loading="busy" class="upload-dialog">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <template v-if="preview">
        <video controls preload="metadata" :src="preview.media_url" aria-label="切片预览" />
        <dl>
          <dt>录像</dt><dd>{{ preview.video }}</dd>
          <dt>标题</dt><dd>{{ preview.work.title }}</dd>
          <dt v-if="preview.work.description">简介</dt><dd v-if="preview.work.description">{{ preview.work.description }}</dd>
          <dt>标签</dt><dd>{{ preview.work.tags.join('、') }}</dd>
          <dt>账号</dt><dd>{{ preview.account.display_name }}（MID {{ preview.account.remote_user_id }}）</dd>
          <dt>可见范围</dt><dd>{{ preview.options.publish_mode === 'self_only' ? '仅自己可见' : '公开' }}</dd>
          <dt>分区</dt><dd>{{ preview.options.category_id }}</dd>
          <dt>版权</dt><dd>{{ preview.options.copyright === 2 ? '转载 · ' + preview.options.source : '自制' }}</dd>
        </dl>
        <el-alert v-for="warning in preview.warnings" :key="warning" :title="warning" type="warning" :closable="false" />
        <el-checkbox v-if="preview.quality_flags.length" v-model="quality">
          已核对质量提示（{{ preview.quality_flags.map(f => qualityFlagLabel(f.flag)).join('、') }}）
        </el-checkbox>
        <div class="actions">
          <el-button :disabled="busy" @click="preview = undefined; error = ''">返回修改</el-button>
          <el-button :disabled="busy" @click="cancel">取消</el-button>
          <el-button type="primary" :loading="busy" :disabled="preview.quality_flags.length > 0 && !quality" @click="confirm">确认安排</el-button>
        </div>
      </template>
      <template v-else-if="detail">
        <p>{{ arrangementStatus(detail) }}</p>
        <p v-if="detail.problem">{{ detail.problem }}</p>
        <div class="actions">
          <el-button :disabled="busy" @click="refresh()">查回原任务</el-button>
          <el-button v-if="detail.can_continue" :disabled="busy" @click="refresh(true)">继续原安排</el-button>
          <el-button v-if="detail.can_revise && segment" :disabled="busy" @click="show(segment, undefined, detail.id)">重新预览</el-button>
        </div>
      </template>
      <template v-else-if="context && params">
        <p>{{ context.filename }}</p>
        <p v-if="context.quality_flags.length">质量提示 · {{ context.quality_flags.map(f => qualityFlagLabel(f.flag)).join('、') }}</p>
        <el-alert v-if="!context.accounts.length" title="当前没有可选账号" type="warning" :closable="false" />
        <router-link v-if="!context.accounts.length" to="/accounts">打开上传平台账号</router-link>
        <el-form label-position="top" :disabled="busy" @submit.prevent="makePreview">
          <el-form-item label="投稿账号">
            <el-select v-model="accountId" placeholder="选择可用账号">
              <el-option v-for="account in context.accounts" :key="account.id" :value="account.id"
                :label="account.display_name + '（MID ' + account.remote_user_id + '）'" />
            </el-select>
          </el-form-item>
          <el-form-item label="标题"><el-input v-model="params.title" /></el-form-item>
          <el-form-item label="简介"><el-input v-model="params.description" type="textarea" /></el-form-item>
          <el-form-item label="标签（用逗号分隔）"><el-input v-model="tags" /></el-form-item>
          <el-form-item label="B 站分区 ID"><el-input-number v-model="params.category_id" :min="1" :max="65535" :precision="0" /></el-form-item>
          <el-form-item label="版权"><el-radio-group v-model="params.copyright">
            <el-radio :value="1">自制</el-radio><el-radio :value="2">转载</el-radio>
          </el-radio-group></el-form-item>
          <el-form-item v-if="params.copyright === 2" label="转载来源"><el-input v-model="params.source" /></el-form-item>
          <el-form-item label="可见范围"><el-radio-group v-model="params.publish_mode">
            <el-radio value="self_only">仅自己可见</el-radio><el-radio value="public">公开</el-radio>
          </el-radio-group></el-form-item>
          <el-button native-type="submit" type="primary" :disabled="!selected || !params.category_id || !params.title.trim()">生成预览</el-button>
        </el-form>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.upload-dialog { min-height: 48px; overflow-wrap: anywhere; }
video { width: 100%; max-height: 32vh; background: #111; margin-top: 12px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; }
dd { margin: 0; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.actions .el-button { margin: 0; }
</style>
