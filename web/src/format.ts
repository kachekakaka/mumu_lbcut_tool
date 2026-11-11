const pad = (n: number) => String(n).padStart(2, '0')

const valid = (sec: number | null | undefined): sec is number => typeof sec === 'number' && Number.isFinite(sec)

/** 浏览器本地时区 `MM-DD HH:mm:ss`，不在今年时带年份。 */
export function fmtTime(sec: number | null | undefined, now: Date = new Date()): string {
  if (!valid(sec)) return '—'
  const d = new Date(sec * 1000)
  const text = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  return d.getFullYear() === now.getFullYear() ? text : `${d.getFullYear()}-${text}`
}

export function fmtClock(sec: number | null | undefined): string {
  if (!valid(sec)) return '—'
  const d = new Date(sec * 1000)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 时长 `时:分:秒`。 */
export function fmtDuration(sec: number | null | undefined): string {
  if (!valid(sec) || sec < 0) return '—'
  const total = Math.floor(sec)
  return `${Math.floor(total / 3600)}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`
}

export function fmtSize(mb: number | null | undefined): string {
  if (!valid(mb)) return '—'
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`
}

export function fmtFps(fps: number | null | undefined): string {
  return valid(fps) ? fps.toFixed(1) : '—'
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
}
