import type { TagType } from './labels'

// 模拟界面的分类：进行中灰色、成功绿色、失败红色；真实枚举由上传专项定义。
const LABEL: Record<string, string> = { pending: '等待上传', uploading: '上传中', succeeded: '已上传', failed: '上传失败' }
const TYPE: Record<string, TagType> = { pending: 'info', uploading: 'info', succeeded: 'success', failed: 'danger' }

export const uploadStatusLabel = (status: string) => LABEL[status] ?? status
export const uploadStatusType = (status: string): TagType => TYPE[status] ?? 'info'
export const UPLOAD_STATUSES = Object.keys(LABEL)
