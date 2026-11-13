import type { Instance } from './api/types'
import { instanceStatusLabel } from './labels'

/** 已启用且存在任务占用、正在开机或关联待对账时，禁止直接停用。 */
export function disableInstanceReason(instance: Instance): string {
  if (!instance.enabled) return ''
  if (instance.status === 'reconcile') return '实例关联待对账任务，请先到任务页对账后再停用'
  if (instance.status === 'booting') return '实例正在开机中，请稍后再停用'
  if (instance.job_id || instance.status === 'busy') return '实例正在录制/占用中，请先停止对应任务后再停用'
  return ''
}

/** 未启用实例保留宿主机真实开/关机状态提示，便于区分本机状态与纳管状态。 */
export function hostPowerHint(instance: Instance): string {
  if (instance.status === 'idle') return '本机已开机'
  if (instance.reason?.code === 'android_not_started') return '本机未开机'
  return instanceStatusLabel(instance.status)
}

export function visibleOverviewInstances(list: Instance[]): Instance[] {
  return list.filter((i) => i.enabled || i.status === 'reconcile')
}

export function disabledInstanceCount(list: Instance[]): number {
  return list.filter((i) => !i.enabled && i.status !== 'reconcile').length
}
