/** Formatting helpers. Durations are integer milliseconds everywhere. */

const DASH = '—'

function pad(value: number, size: number): string {
  return String(value).padStart(size, '0')
}

/**
 * SPEC section 2 round-trip format: `28872 -> "28.872"`, `62345 -> "1:02.345"`.
 * Used in tables, tooltips outside the lap chart, and deltas.
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return DASH
  const sign = ms < 0 ? '-' : ''
  const total = Math.round(Math.abs(ms))
  const millis = total % 1000
  const seconds = Math.floor(total / 1000) % 60
  const minutes = Math.floor(total / 60_000)
  if (minutes === 0) return `${sign}${seconds}.${pad(millis, 3)}`
  return `${sign}${minutes}:${pad(seconds, 2)}.${pad(millis, 3)}`
}

/** Fixed-width `mm:ss.mmm` — the lap-time chart axis and its tooltip. */
export function formatClock(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return DASH
  const sign = ms < 0 ? '-' : ''
  const total = Math.round(Math.abs(ms))
  const millis = total % 1000
  const seconds = Math.floor(total / 1000) % 60
  const minutes = Math.floor(total / 60_000)
  return `${sign}${pad(minutes, 2)}:${pad(seconds, 2)}.${pad(millis, 3)}`
}

/** Signed duration, e.g. a gap or a median difference: `+2.022` / `-0.315`. */
export function formatSignedDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return DASH
  const sign = ms > 0 ? '+' : ms < 0 ? '-' : '±'
  return `${sign}${formatDuration(Math.abs(ms))}`
}

/** Classification gap: either a time behind the leader or whole laps. */
export function formatGap(gapMs: number | null, gapLaps: number | null): string {
  if (gapLaps !== null && gapLaps > 0) return `${gapLaps} ${gapLaps === 1 ? 'lap' : 'laps'}`
  if (gapMs === null) return DASH
  if (gapMs === 0) return '—'
  return `+${formatDuration(gapMs)}`
}

export function formatSignedMillis(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}`
}

export function formatPercent(ratio: number | null | undefined, digits = 2): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return DASH
  return `${(ratio * 100).toFixed(digits)}%`
}

export function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH
  return value.toFixed(digits)
}

/** p-values never pretend to more precision than they have. */
export function formatPValue(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return DASH
  if (p < 0.001) return '<0.001'
  return p.toFixed(3)
}

/** Naive local timestamp from the API: `2026-08-03T21:40:00` -> `03.08.2026 21:40`. */
export function formatSessionDate(value: string | null | undefined): string {
  if (!value) return DASH
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(value)
  if (match === null) return value
  const [, year, month, day, hour, minute] = match
  const date = `${day}.${month}.${year}`
  return hour !== undefined && minute !== undefined ? `${date} ${hour}:${minute}` : date
}
