/**
 * The single door to the REST API (SPEC section 8.4).
 *
 * Every request goes through `request()`, so an offline backend is detected in
 * one place: `ApiOfflineError` is what the app turns into demo mode.
 * All durations are integer milliseconds, exactly as the API sends them.
 */

export const API_BASE = '/api'

// --------------------------------------------------------------------------
// Wire types
// --------------------------------------------------------------------------

export interface SessionSummary {
  id: number
  name: string
  code: string | null
  started_at: string | null
  track: string | null
  category: string | null
  club: string | null
  drivers_count: number
  laps_count: number
}

export interface SessionInfo {
  id?: number
  name: string
  code: string | null
  started_at: string | null
  track: string | null
  category: string | null
  tz_name?: string | null
}

export interface ClubInfo {
  name: string
  external_id?: string | null
  website?: string | null
  email?: string | null
}

export interface EntryRow {
  driver: string
  position: number | null
  kart: string | null
  laps_count: number | null
  gap_ms: number | null
  gap_laps: number | null
  best_lap_ms: number | null
}

/** Where a lap annotation came from (SPEC section 10.3). */
export type TagSource = 'auto' | 'manual'

/** One row of `lap_annotation` as the API sends it. */
export interface LapAnnotation {
  tag: string
  /** Missing on older payloads; only the detector ever writes `auto`. */
  source?: string | null
  note?: string | null
  created_at?: string | null
}

/** A lap tag on the wire: a bare value, or the annotation row behind it. */
export type LapTagRef = string | LapAnnotation

export interface LapRow {
  id: number
  driver: string
  lap_number: number
  time_ms: number | null
  sectors: (number | null)[]
  is_best: boolean
  /** Tags in force: a manual annotation voids the automatic ones for that lap. */
  tags: LapTagRef[]
  /** Every `lap_annotation` row of the lap, overridden ones included. */
  annotations?: LapAnnotation[]
  /** Split by origin; absent on payloads that only carry `tags`. */
  manual_tags?: string[]
  auto_tags?: string[]
  /** Tags the filters honour — a manual annotation voids the automatic ones. */
  effective_tags?: string[]
  manually_annotated?: boolean
}

export interface SessionDetail {
  session: SessionInfo
  club: ClubInfo | null
  entries: EntryRow[]
  laps: LapRow[]
}

/** Client-side state of the lap filter; serialised into query parameters. */
export interface LapFilterState {
  mad_k: number
  drop_first_lap: boolean
  drop_slow_outliers: boolean
  drop_fast_outliers: boolean
  exclude_tags: string[]
  min_laps: number
}

/**
 * SPEC section 10.4: `joker` and `pit` are excluded by default — both laps are
 * mandatory in this format and neither is race pace. `drop_fast_outliers` stays
 * off because the joker tag now removes the fast lap for a stated reason.
 */
export const DEFAULT_FILTER: LapFilterState = {
  mad_k: 3,
  drop_first_lap: true,
  drop_slow_outliers: true,
  drop_fast_outliers: false,
  exclude_tags: ['penalty', 'boost', 'joker', 'pit', 'invalid', 'outlier'],
  min_laps: 3,
}

/** `karting.stats.LapFlags` — why a lap was kept out of the sample. */
export interface LapFlag {
  lap_number: number
  used: boolean
  reason: string | null
  suspicious_fast: boolean
}

/** `karting.stats.PaceStats`. */
export interface PaceStats {
  n_laps: number
  n_used: number
  best_ms: number | null
  median_ms: number | null
  mean_ms: number | null
  trimmed_mean_ms: number | null
  std_ms: number | null
  iqr_ms: number | null
  mad_ms: number | null
  cv: number | null
  consistency: number | null
  theoretical_best_ms: number | null
  degradation_ms_per_lap: number | null
  degradation_p_value: number | null
  used_lap_numbers: number[]
  excluded: LapFlag[]
}

export interface DriverStats extends PaceStats {
  driver: string
  position: number | null
  kart?: string | null
  /** Gap to the fastest driver's median pace, in ms; `null` when unknown. */
  pace_delta_to_best_ms?: number | null
  /** Laps kept in the sample but flagged as suspiciously fast (SPEC section 6). */
  suspicious_fast_lap_numbers?: number[]
  /**
   * Official-vs-clean block (SPEC section 10.4). Optional on the wire: when the
   * server does not send it, `buildBestLapRows` derives the same numbers from
   * the classification and the laps.
   */
  official_best_ms?: number | null
  official_best_source?: string | null
  official_best_lap_number?: number | null
  official_best_lap_id?: number | null
  official_best_tags?: string[]
  official_best_is_joker?: boolean
  /** `best_ms - official_best_ms`: positive when the official time is a lie. */
  best_delta_ms?: number | null
}

/** `karting.stats.LapFilter` as the server echoes it back. */
export interface AppliedFilter {
  exclude_tags?: string[]
  mad_k?: number
  drop_missing?: boolean
  drop_first_lap?: boolean
  drop_slow_outliers?: boolean
  drop_fast_outliers?: boolean
  min_laps?: number
}

/** Session-level summary of the official-vs-clean contrast (SPEC section 10.4). */
export interface OfficialBestSummary {
  joker_inflated_drivers: string[]
  joker_inflated_count: number
  drivers_count: number
  label: string
  note: string
}

export interface StatsResponse {
  filter: AppliedFilter
  drivers: DriverStats[]
  official_best?: OfficialBestSummary
}

/** `karting.stats.TestResult`. */
export interface TestResult {
  name: string
  statistic: number | null
  p_value: number | null
  ci_low: number | null
  ci_high: number | null
  effect_size: number | null
  effect_name: string | null
  interpretation: string
}

/** `karting.stats.DriverComparison`. */
export interface DriverComparison {
  driver_a: string
  driver_b: string
  stats_a: PaceStats
  stats_b: PaceStats
  n_a: number
  n_b: number
  mean_diff_ms: number | null
  median_diff_ms: number | null
  tests: TestResult[]
  caveats: string[]
}

export interface TagOption {
  value: string
  label: string
}

// --------------------------------------------------------------------------
// Joker / pit detection (SPEC section 10)
// --------------------------------------------------------------------------

/** The two mandatory laps of this race format; neither one is pace. */
export const JOKER_TAG = 'joker'
export const PIT_TAG = 'pit'

/** `karting.stats.events.EventDetectionConfig`, echoed by the API. */
export interface EventConfigInfo {
  pit_ratio: number
  joker_ratio: number
  one_per_driver: boolean
  require_single_sector: boolean
  skip_first_lap: boolean
}

/** `karting.stats.events.DetectedEvent` plus the storage state of its tag. */
export interface DetectedEvent {
  driver: string
  lap_number: number
  /** `joker` or `pit`. */
  kind: string
  /** lap time / the driver's robust baseline. */
  ratio: number | null
  delta_ms: number | null
  /** 0-based index of the sector holding the anomaly, when confirmed. */
  sector_index: number | null
  confidence: number | null
  note: string
  time_ms?: number | null
  lap_id?: number | null
  /** The matching tag is in force on that lap right now. */
  applied?: boolean
  /** A human annotated that lap, so the detector is ignored for it. */
  overridden_by_manual?: boolean
}

/** `karting.stats.events.EventReport` as `GET /api/sessions/{id}/events` sends it. */
export interface EventReport {
  session_id: number
  config: EventConfigInfo
  events: DetectedEvent[]
  drivers_without_joker: string[]
  drivers_without_pit: string[]
  drivers_with_multiple: string[]
  /**
   * One proposal per driver of `drivers_without_pit`: their slowest lap, with
   * `confidence = 0` because the detector suggests rather than claims it. The
   * pit stop is mandatory, so a missing one always arrives with a lap to
   * confirm (SPEC section 10.2).
   */
  pit_candidates?: DetectedEvent[]
  warnings: string[]
  counts: Record<string, number>
  persisted?: boolean
  /** Every driver has exactly one joker and exactly one pit. */
  complete?: boolean
}

/** One row of the "Лучшие времена недели" / "Track records" leaderboards. */
export interface RankingRow {
  rank: number
  driver: string
  best_lap_ms: number | null
  category?: string | null
}

/**
 * `GET /api/sessions/{id}/rankings`. The leaderboards are copied verbatim from
 * the email, so they rank drivers by the official best lap — a joker lap for
 * five of the six drivers of the reference race (SPEC section 10.4).
 */
export interface RankingsResponse {
  weekly_best: RankingRow[]
  track_record: RankingRow[]
  official_best_based?: boolean
  joker_inflated?: boolean
  label?: string
  note?: string
}

export type ImportStatus = 'imported' | 'merged' | 'already_imported' | 'failed'

/** `karting.api.schemas.ImportReportOut` — one report per uploaded file. */
export interface ImportReport {
  filename: string
  status: ImportStatus
  detail: string
  session_id: number | null
  club_id: number | null
  session_created: boolean
  already_imported: boolean
  inserted_laps: number
  updated_laps: number
  inserted_entries: number
  /** Joker / pit laps the detector tagged during this import (SPEC section 10.3). */
  auto_jokers?: number
  auto_pits?: number
  drivers_without_joker?: string[]
  drivers_without_pit?: string[]
  conflicts: string[]
  warnings: string[]
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

/** The backend could not be reached at all (connection refused, DNS, CORS). */
export class ApiOfflineError extends Error {
  constructor(cause: unknown) {
    super('The API is not reachable')
    this.name = 'ApiOfflineError'
    this.cause = cause
  }
}

/** The backend answered with a non-2xx status. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

// --------------------------------------------------------------------------
// Transport
// --------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (isRecord(body) && typeof body.detail === 'string') return body.detail
  } catch {
    // Body was not JSON; fall through to the status text.
  }
  return `${response.status} ${response.statusText}`.trim()
}

/**
 * Whether a rejection is the caller cancelling their own request.
 *
 * `useAsync` aborts the in-flight request whenever its dependencies change (and
 * React StrictMode makes that happen on the very first render in development),
 * so an abort says nothing at all about the backend. Reporting it as "offline"
 * would flip the whole dashboard to bundled demo data while the API is
 * answering 200 to every call.
 */
export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  return error instanceof Error && error.name === 'AbortError'
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, init)
  } catch (error) {
    if (isAbortError(error)) throw error
    throw new ApiOfflineError(error)
  }
  // A dev proxy with no upstream answers 502/504 — that is "offline" too.
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    throw new ApiOfflineError(await readDetail(response))
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response))
  }
  return response
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await (await send(path, init)).json()) as T
}

/** For the `204 No Content` endpoints: there is no body to parse. */
async function requestVoid(path: string, init?: RequestInit): Promise<void> {
  await send(path, init)
}

/** Serialises the lap filter into the query parameters shared by /stats and /compare. */
export function filterParams(filter: LapFilterState): URLSearchParams {
  const params = new URLSearchParams()
  params.set('mad_k', String(filter.mad_k))
  params.set('drop_first_lap', String(filter.drop_first_lap))
  params.set('drop_slow_outliers', String(filter.drop_slow_outliers))
  params.set('drop_fast_outliers', String(filter.drop_fast_outliers))
  params.set('min_laps', String(filter.min_laps))
  params.set('exclude_tags', filter.exclude_tags.join(','))
  return params
}

// --------------------------------------------------------------------------
// Endpoints
// --------------------------------------------------------------------------

export function listSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  return request<SessionSummary[]>('/sessions', { signal })
}

export function getSession(id: number, signal?: AbortSignal): Promise<SessionDetail> {
  return request<SessionDetail>(`/sessions/${id}`, { signal })
}

export function getStats(
  id: number,
  filter: LapFilterState,
  signal?: AbortSignal,
): Promise<StatsResponse> {
  return request<StatsResponse>(`/sessions/${id}/stats?${filterParams(filter).toString()}`, {
    signal,
  })
}

export function getComparison(
  id: number,
  a: string,
  b: string,
  filter: LapFilterState,
  signal?: AbortSignal,
): Promise<DriverComparison> {
  const params = filterParams(filter)
  params.set('a', a)
  params.set('b', b)
  return request<DriverComparison>(`/sessions/${id}/compare?${params.toString()}`, { signal })
}

export function getTags(signal?: AbortSignal): Promise<TagOption[]> {
  return request<TagOption[]>('/tags', { signal })
}

export function getRankings(id: number, signal?: AbortSignal): Promise<RankingsResponse> {
  return request<RankingsResponse>(`/sessions/${id}/rankings`, { signal })
}

/**
 * Joker / pit detection report of a session.
 *
 * Resolves to `null` when the backend does not serve the endpoint (a build
 * older than SPEC section 10): the events panel then falls back to the tags
 * carried by the laps themselves, which is the part of the contract that has
 * always existed.
 */
export async function getEvents(id: number, signal?: AbortSignal): Promise<EventReport | null> {
  try {
    return await request<EventReport>(`/sessions/${id}/events`, { signal })
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 405)) return null
    throw error
  }
}

/** Attach a manual tag to a lap. A human decision always beats the detector. */
export function addLapTag(lapId: number, tag: string, note?: string | null): Promise<void> {
  return requestVoid(`/laps/${lapId}/tags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, note: note ?? null }),
  })
}

export function removeLapTag(lapId: number, tag: string): Promise<void> {
  return requestVoid(`/laps/${lapId}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' })
}

export function uploadEmails(files: File[]): Promise<ImportReport[]> {
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  return request<ImportReport[]>('/imports', { method: 'POST', body: form })
}
