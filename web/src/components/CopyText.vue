<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { copyText } from '../clipboard'

const props = defineProps<{ text: string | null; display?: string }>()

async function copy() {
  if (!props.text) return
  if (await copyText(props.text)) {
    ElMessage.success('已复制')
  } else {
    await ElMessageBox.alert(props.text, '请手动复制', { confirmButtonText: '关闭' }).catch(() => undefined)
  }
}
</script>

<template>
  <span class="copy-text">
    <span class="copy-value" :title="text ?? ''">{{ display ?? text ?? '—' }}</span>
    <el-button v-if="text" link type="primary" size="small" @click.stop="copy">复制</el-button>
  </span>
</template>
