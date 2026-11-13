import { request, type Query } from './client'
import type { Command, Instance, Job, JobDetail, JobLog, OpenFolderResponse, Overview, Page, Receipt, Segment, SystemInfo, Target } from './types'

const get = <T>(path: string, query?: Query) => request<T>('GET', path, { query }).then((r) => r.data)
const id = (value: string) => encodeURIComponent(value)

function write(method: string, path: string, requestId: string, body?: unknown) {
  return request<Receipt>(method, path, { body, headers: { 'Idempotency-Key': requestId }, timeoutMs: 15000 }).then((r) => r.data)
}

export const api = {
  system: () => get<SystemInfo>('/api/system'),
  overview: (q: Query) => get<Overview>('/api/overview', q),
  instances: (q: Query) => get<Page<Instance>>('/api/instances', q),
  targets: (q: Query) => get<Page<Target>>('/api/targets', q),
  jobs: (q: Query) => get<Page<Job>>('/api/jobs', q),
  job: (jobId: string) => get<JobDetail>(`/api/jobs/${id(jobId)}`),
  jobLog: (jobId: string, lines: number) => get<JobLog>(`/api/jobs/${id(jobId)}/log`, { lines }),
  segments: (q: Query) => get<Page<Segment>>('/api/segments', q),
  command: (commandId: string) => get<Command>(`/api/commands/${id(commandId)}`),

  addTargets: (targets: string[], manualOnly: boolean, requestId: string) =>
    write('POST', '/api/targets', requestId, { targets, manual_only: manualOnly, allow_unconfirmed_identity: false }),
  removeTarget: (targetId: string, requestId: string) => write('DELETE', `/api/targets/${id(targetId)}`, requestId),
  setAutoRecord: (targetId: string, autoRecord: boolean, requestId: string) =>
    write('PATCH', `/api/targets/${id(targetId)}`, requestId, { auto_record: autoRecord }),
  setInstanceEnabled: (vmindex: number, enabled: boolean, requestId: string) =>
    write('PATCH', `/api/instances/${vmindex}`, requestId, { enabled }),
  record: (targetId: string, requestId: string) => write('POST', `/api/targets/${id(targetId)}/record`, requestId, {}),
  stopTarget: (targetId: string, sessionKey: string, requestId: string) =>
    write('POST', `/api/targets/${id(targetId)}/stop`, requestId, { session_key: sessionKey }),
  stopJob: (jobId: string, requestId: string) => write('POST', `/api/jobs/${id(jobId)}/stop`, requestId, {}),
  openSegmentFolder: (jobId: string, segmentIndex: number) =>
    request<OpenFolderResponse>('POST', `/api/segments/${id(jobId)}/${segmentIndex}/open-folder`, { timeoutMs: 10000 }).then((r) => r.data),
}
