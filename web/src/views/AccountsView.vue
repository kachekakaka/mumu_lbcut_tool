<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, reactive } from 'vue'
import { request, pending, remember, forget, bindDeployment, LoginFailure, type Intent } from '../api/bilibili'

interface Summary {id:string; display_name:string; remote_user_id:string; availability:string; block_reasons:string[]}
interface Login {session_id:string; account_id:string|null; revision:number; phase:string; outcome:string|null; can_resume:boolean; can_retry:boolean; can_cancel:boolean; challenge:{challenge_id:string;deadline_at:string}|null;error:{code:string}|null}
interface Op { key:string; action:string; target:string|null; session:string|null; status:string; error?:string }
const local = ['127.0.0.1','[::1]'].includes(location.hostname)
const status = ref({state:'loading',deployment:'',available:false})
const form = reactive({python:'',data_dir:'',engine_config:'',port:8765,create:false})
const edit = ref(false), error = ref(''), busy = ref(false), paused = ref(false)
const accounts = ref<Summary[]>([]), sessions = ref<Login[]>([]), operations = ref<Op[]>([])
const intent = ref<Intent|null>(null), dialog = ref(false), sid = ref(''), current = ref<Login|null>(null)
const img = ref(''), challenge = ref(''), note = ref('')
const states:Record<string,string> = {loading:'正在连接',unconfigured:'尚未配置',stopped:'服务未启动',online:'已连接',starting:'正在启动',occupied:'端口被未确认服务占用',check:'启动记录待核对'}
const phases:Record<string,string> = {queued:'已排队',starting:'正在准备二维码',waiting_for_executions:'等待原执行结束',waiting_confirmation:'等待手机确认',verifying_identity:'正在核验身份',waiting_for_account:'等待原账号',finalizing:'正在保存登录结果',cancelling:'正在取消',recovering:'原操作待恢复',finished:'已结束',succeeded:'登录成功',failed:'登录失败',expired:'已失效',cancelled:'已取消'}
const availability:Record<string,string> = {ready:'可用',disabled:'已手动停用',blocked:'已阻塞'}
let alive = true, epoch = 0, timer: ReturnType<typeof setTimeout>, expiry: ReturnType<typeof setTimeout>, failures = 0
let refreshTask:Promise<void>|undefined
const hiddenChallenges=new Set<string>()
function hideCurrentChallenge() {
  if(current.value?.challenge) hiddenChallenges.add(sid.value+'/'+current.value.challenge.challenge_id)
  clearImage()
}
function clearImage() {
  clearTimeout(expiry)
  if(img.value) URL.revokeObjectURL(img.value)
  img.value=''; challenge.value=''
}
function imageLoaded(event:Event) {
  const url=(event.currentTarget as HTMLImageElement).currentSrc
  if(url.startsWith('blob:')) URL.revokeObjectURL(url)
}
function close() { ++epoch; dialog.value=false; clearImage(); current.value=null; sid.value='' }
function observe(id:string) {
  close(); sid.value=id; dialog.value=true; note.value=''; void refresh()
}
function showError(e:unknown) {
  error.value=e instanceof Error ? e.message : '账号服务中断'
  if(e instanceof LoginFailure && ['local_only','reconnect'].includes(e.code)) paused.value=true
}
async function load() {
  const stamp=epoch, id=sid.value
  const valid=()=>alive && epoch===stamp
  try {
    const s=await request('/status')
    if(!valid()) return
    const changed=status.value.deployment && status.value.deployment!==s.deployment
    bindDeployment(s.deployment || ''); status.value=s
    if(changed) { close(); accounts.value=[]; operations.value=[]; sessions.value=[]; return }
    if(s.state!=='online') { clearImage(); return }
    if(id && dialog.value) {
      const v:Login=await request('/sessions/'+encodeURIComponent(id))
      if(!valid() || v.session_id!==id) return
      const prev=current.value
      if(prev && (v.revision<prev.revision || (prev.outcome && !v.outcome))) return
      if(prev && prev.revision===v.revision && (prev.outcome!==v.outcome || prev.phase!==v.phase)) throw Error('会话状态矛盾，请重新核对')
      current.value=v
      const c=v.challenge, until=c ? Date.parse(c.deadline_at) : 0
      if(prev?.challenge && (!c || prev.challenge.challenge_id!==c.challenge_id)) hiddenChallenges.add(id+'/'+prev.challenge.challenge_id)
      if(!c || v.outcome || v.phase!=='waiting_confirmation' || !(until>Date.now()) || hiddenChallenges.has(id+'/'+c.challenge_id)) clearImage()
      else if(c.challenge_id!==challenge.value) {
        clearImage()
        const blob=await request('/sessions/'+encodeURIComponent(id)+'/qr?challenge_id='+encodeURIComponent(c.challenge_id),undefined,undefined,true)
        if(!valid() || until<=Date.now()) return
        img.value=URL.createObjectURL(blob); challenge.value=c.challenge_id
        expiry=setTimeout(hideCurrentChallenge,until-Date.now())
      }
    }
    const [ops, active, summaries]=await Promise.all([request('/operations'),request('/sessions'),request('/accounts')])
    if(!valid()) return
    operations.value=ops.items; sessions.value=active.items; accounts.value=summaries.items
    if(intent.value && intent.value.deployment===s.deployment) {
      const known:Op|undefined=ops.items.find((o:Op)=>o.key===intent.value?.key)
      if(known && ['done','linked','rejected'].includes(known.status)) { forget(); intent.value=null }
    }
    error.value=''; failures=0
  } catch(e) { if(valid()) { clearImage(); accounts.value=[]; status.value.available=false; showError(e); failures++ } }
}
function refresh() {
  if(refreshTask) return refreshTask
  clearTimeout(timer)
  refreshTask=load().finally(()=>{
    refreshTask=undefined
    if(alive && !paused.value) timer=setTimeout(refresh,failures>1?5000:failures?2000:1000)
  })
  return refreshTask
}
async function run(fn:()=>Promise<void>) {
  if(busy.value) return
  busy.value=true; error.value=''
  try { await fn() } catch(e) { showError(e) } finally { busy.value=false }
}
function configure() { void run(async()=>{ await request('/config',form,crypto.randomUUID()); edit.value=false; await refresh() }) }
function openSettings() { void run(async()=>{ Object.assign(form,await request('/config'),{create:false}); edit.value=true }) }
function ensure() { void run(async()=>{ status.value=await request('/ensure',{},crypto.randomUUID()); await refresh() }) }
function act(action:string,target:string|null, existing?:Intent) { void run(async()=>{
  if(intent.value && !existing) throw Error('请先核对待确认操作')
  const op=existing || {action,target,key:crypto.randomUUID(),deployment:status.value.deployment}
  if(op.deployment!==status.value.deployment) throw Error('请连接原部署再继续')
  try { remember(op) } catch { throw Error('浏览器无法保存请求编号，请启用会话存储后再操作') }
  intent.value=op
  const stamp=epoch
  if(action==='cancel') { ++epoch; hideCurrentChallenge(); note.value='正在核对取消结果' }
  const reply=await request('/operations',{action:op.action,target:op.target,deployment:op.deployment},op.key)
  if(!alive) return
  if(reply.status==='rejected') { forget(); intent.value=null; throw Error(reply.error || '请求被拒绝') }
  if(reply.session) {
    forget(); intent.value=null
    if(epoch===stamp || (action==='cancel' && dialog.value && sid.value===reply.session)) observe(reply.session)
  } else { note.value='提交结果待确认'; if(reply.key!==op.key) { forget(); intent.value=null } }
  await refresh()
}) }
onMounted(()=>{ if(local) { try { intent.value=pending() } catch { error.value='无法读取浏览器会话存储' } void refresh() } })
onBeforeUnmount(()=>{ alive=false; close(); clearTimeout(timer) })
</script>
<template>
  <h2>Bilibili 账号</h2>
  <el-alert v-if="!local" title="请在服务所在电脑打开本机控制台" type="info" :closable="false"/>
  <template v-else>
    <p>登录服务 · {{ status.state==='failed' ? '启动失败，请核对 Core 环境与配置' : states[status.state] || status.state }}
      <el-button v-if="['unconfigured','stopped','failed'].includes(status.state) || error" @click="openSettings">配置部署</el-button>
      <el-button v-if="['stopped','failed'].includes(status.state)" :loading="busy" @click="ensure">启动登录服务</el-button>
      <el-button @click="paused=false;refresh()">重新连接</el-button>
    </p>
    <p v-if="status.state==='check'">请由本机运维核对原启动进程。若身份记录不全，请安排维护窗口重启 Windows 后重新连接。</p>
    <el-alert v-if="error" :title="error" type="warning" :closable="false"/>
    <el-button v-if="intent" :disabled="busy" @click="act(intent.action,intent.target,intent)">重试原请求</el-button>
    <template v-if="status.state==='online'">
      <p v-if="!status.available">当前引擎无法新建扫码，请核对部署能力。已有会话仍可查看。</p>
      <el-button type="primary" :disabled="!status.available || busy || !!intent" @click="act('create',null)">添加 Bilibili 账号</el-button>
      <el-table :data="accounts" empty-text="暂无账号">
        <el-table-column prop="display_name" label="昵称"/><el-table-column prop="remote_user_id" label="MID"/>
        <el-table-column label="账号状态"><template #default="{row}">{{availability[row.availability]}} {{row.block_reasons.join('、')}}</template></el-table-column>
        <el-table-column label="操作"><template #default="{row}"><el-button :disabled="busy || !status.available || !!intent" @click="act('create',row.id)">重新登录</el-button></template></el-table-column>
      </el-table>
      <p v-for="s in sessions" :key="s.session_id"><el-button @click="observe(s.session_id)">查看登录进度 · {{ phases[s.phase] }}</el-button></p>
      <details :open="operations.some(o=>['unknown','sending'].includes(o.status))"><summary>登录操作记录</summary>
        <p v-for="o in operations" :key="o.key">{{ {create:'申请登录',cancel:'取消',resume:'恢复'}[o.action] }} · {{o.error || ({done:'已处理',linked:'已关联',unknown:'待确认',sending:'待核对',rejected:'被拒绝'}[o.status])}}
          <el-button v-if="o.session" @click="observe(o.session)">查看结果</el-button>
          <el-button v-else-if="['unknown','sending'].includes(o.status)" :disabled="busy" @click="act(o.action,o.target,{...o,deployment:status.deployment})">重试原请求</el-button>
        </p>
      </details>
    </template>
    <el-dialog v-model="edit" title="本机 Core 部署">
      <el-form label-position="top"><el-form-item label="Core 虚拟环境 python.exe"><el-input v-model="form.python"/></el-form-item>
        <el-form-item label="Core 数据目录"><el-input v-model="form.data_dir"/></el-form-item>
        <el-checkbox v-model="form.create">新建独立部署（使用尚不存在的目录）</el-checkbox>
        <el-form-item label="Bilibili 引擎配置文件"><el-input v-model="form.engine_config"/></el-form-item>
        <el-form-item label="本机 Core 端口"><el-input-number v-model="form.port" :min="1" :max="65535"/></el-form-item>
      </el-form><p v-if="error">{{error}}</p><el-button type="primary" :loading="busy" @click="configure">保存部署</el-button>
    </el-dialog>
    <el-dialog v-model="dialog" title="扫码登录" @close="close">
      <p>{{ phases[current?.outcome || current?.phase || ''] || '正在查询原会话' }}</p>
      <img v-if="img" :src="img" @load="imageLoaded" alt="请用哔哩哔哩手机客户端扫码" style="max-width:100%;width:260px;image-rendering:pixelated"/>
      <p v-if="current?.outcome==='succeeded' && error">登录成功，账号可用性待刷新。</p>
      <p v-else-if="current?.outcome==='succeeded'">账号可用性请查看最新账号列表。</p>
      <p v-if="note || error || current?.error">{{error || current?.error?.code || note}}</p>
      <p>关闭窗口后，可从账号页继续查看本次登录。</p>
      <el-button v-if="current?.can_cancel" :disabled="busy" @click="act('cancel',sid)">取消登录</el-button>
      <el-button v-if="current?.can_resume" :disabled="busy" @click="act('resume',sid)">恢复原操作</el-button>
      <el-button v-if="current?.can_retry" :disabled="busy || !status.available" @click="act('create',current.account_id)">重新申请二维码</el-button>
    </el-dialog>
  </template>
</template>
