import { ElMessage } from 'element-plus'
import { api } from './api'
import { createOperations, describeOperation } from './commands'
import { newRequestId } from './ids'
import { refreshNow } from './poller'

export const ops = createOperations({
  getCommand: api.command,
  newId: newRequestId,
  refresh: refreshNow,
  notify(op) {
    const d = describeOperation(op)
    ElMessage({ type: d.level, message: `${op.title}：${d.text}`, duration: d.level === 'success' ? 3000 : 6000, showClose: true })
  },
})
