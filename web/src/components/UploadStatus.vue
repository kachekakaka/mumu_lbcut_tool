<script setup lang="ts">
// 上传状态只在后端声明启用上传时显示；当前仅有开发模拟数据，状态分类以后续上传专项为准。
import type { MockUploadState } from '../api/types'
import { fmtTime } from '../format'
import { uploadStatusLabel, uploadStatusType } from '../upload'

defineProps<{ upload?: MockUploadState | null }>()
</script>

<template>
  <span v-if="!upload" class="muted">—</span>
  <el-tooltip
    v-else
    placement="top"
    :content="`尝试 ${upload.attempts} 次；更新 ${fmtTime(upload.updated_at)}${upload.last_error ? `；最近错误：${upload.last_error}` : ''}`"
  >
    <el-tag :type="uploadStatusType(upload.status)" size="small">{{ uploadStatusLabel(upload.status) }}</el-tag>
  </el-tooltip>
</template>
