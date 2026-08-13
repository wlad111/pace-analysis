/**
 * Offline demo backend.
 *
 * Serves the endpoints the dashboard needs from `session.json` (generated from
 * the hand-verified fixture by `scripts/build-mock.mjs`), so the page is fully
 * usable — filters, joker/pit review and manual tagging included — before the
 * real API is up. Tag changes are kept in memory for the life of the tab: the
 * demo has nowhere to persist them, but the reader still sees what a manual
 * override does to the clean best lap.
 */

import type {
  DetectedEvent,
  DriverComparison,
  DriverStats,
  EventReport,
  LapAnnotation,
  LapFilterState,
  LapRow,
  RankingsResponse,
  SessionDetail,
  SessionSummary,
  StatsResponse,
  TagOption,
} from '../api'
import { ApiError } from '../api'
import { buildEventModel, buildBestLapRows, effectiveTagsOf } from '../events'

import type { LapPoint } from './statistics'
import { classifyLaps, compareDrivers, paceStats } from './statistics'
import payload from './session.json'

interface DemoPayload {
  sessions: SessionSummary[]
  session: SessionDetail
  tags: TagOption[]
  events: EventReport
  rankings: RankingsResponse
}

const data = payload as unknown as DemoPayload

/** Lap id -> annotations, seeded with the automatic joker / pit tags. */
const annotations = new Map<number, LapAnnotation[]>(
  data.session.laps.map((lap) => [
    lap.id,
    (lap.annotations ?? lap.tags).map((tag) =>
      typeof tag === 'string' ? { tag, source: 'auto' } : { ...tag },
    ),
  ]),
)

function tagsOf(lapId: number): LapAnnotation[] {
  return annotations.get(lapId) ?? []
}

/** Rebuilds a lap with the current tag state, mirroring `normalise_lap`. */
function lapWithTags(lap: LapRow): LapRow {
  const current = tagsOf(lap.id)
  const manual = current.filter((item) => item.source !== 'auto').map((item) => item.tag)
  const auto = current.filter((item) => item.source === 'auto').map((item) => item.tag)
  const effective = manual.length > 0 ? manual : [...new Set([...manual, ...auto])]
  return {
    ...lap,
    annotations: current,
    tags: current.filter((item) => manual.length === 0 || item.source !== 'auto'),
    manual_tags: [...new Set(manual)],
    auto_tags: [...new Set(auto)],
    effective_tags: [...new Set(effective)],
    manually_annotated: manual.length > 0,
  }
}

function lapsOf(detail: SessionDetail, driver: string): LapPoint[] {
  return detail.laps
    .filter((lap) => lap.driver === driver)
    .map((lap) => ({
      lap_number: lap.lap_number,
      time_ms: lap.time_ms,
      sectors: lap.sectors,
      tags: effectiveTagsOf(lap),
    }))
    .sort((a, b) => a.lap_number - b.lap_number)
}

function detailFor(sessionId: number): SessionDetail {
  const known = data.session.session.id ?? data.sessions[0]?.id
  if (known !== undefined && sessionId !== known) {
    throw new ApiError(404, `Session ${sessionId} is not part of the demo data`)
  }
  return { ...data.session, laps: data.session.laps.map(lapWithTags) }
}

export function demoSessions(): Promise<SessionSummary[]> {
  return Promise.resolve(data.sessions)
}

export function demoSession(sessionId: number): Promise<SessionDetail> {
  return Promise.resolve(detailFor(sessionId))
}

export function demoTags(): Promise<TagOption[]> {
  return Promise.resolve(data.tags)
}

export function demoRankings(sessionId: number): Promise<RankingsResponse> {
  detailFor(sessionId)
  return Promise.resolve(data.rankings)
}

/**
 * The detector report as generated offline, with the storage flags refreshed:
 * a lap a human annotated by hand no longer honours the automatic proposal
 * (SPEC section 10.3).
 */
export function demoEvents(sessionId: number): Promise<EventReport> {
  const detail = detailFor(sessionId)
  const byLapNumber = new Map(detail.laps.map((lap) => [`${lap.driver}#${lap.lap_number}`, lap]))
  const events: DetectedEvent[] = data.events.events.map((event) => {
    const lap = byLapNumber.get(`${event.driver}#${event.lap_number}`)
    return {
      ...event,
      applied: lap !== undefined && effectiveTagsOf(lap).includes(event.kind),
      overridden_by_manual: lap?.manually_annotated ?? false,
    }
  })
  const model = buildEventModel(detail.laps, detail.entries.map((entry) => entry.driver), null)
  return Promise.resolve({
    ...data.events,
    events,
    drivers_without_joker: model.driversWithoutJoker,
    drivers_without_pit: model.driversWithoutPit,
    counts: { drivers: model.rows.length, joker: model.jokers, pit: model.pits },
    complete: model.driversWithoutJoker.length === 0 && model.driversWithoutPit.length === 0,
  })
}

export function demoStats(sessionId: number, filter: LapFilterState): Promise<StatsResponse> {
  const detail = detailFor(sessionId)
  const stats = detail.entries.map((entry) => ({
    entry,
    pace: paceStats(lapsOf(detail, entry.driver), filter),
    flags: classifyLaps(lapsOf(detail, entry.driver), filter).flags,
  }))
  const medians = stats
    .map((item) => item.pace.median_ms)
    .filter((value): value is number => value !== null)
  const reference = medians.length > 0 ? Math.min(...medians) : null
  const drivers: DriverStats[] = stats.map(({ entry, pace, flags }) => ({
    driver: entry.driver,
    position: entry.position,
    kart: entry.kart,
    ...pace,
    pace_delta_to_best_ms:
      pace.median_ms === null || reference === null ? null : pace.median_ms - reference,
    suspicious_fast_lap_numbers: flags
      .filter((flag) => flag.suspicious_fast)
      .map((flag) => flag.lap_number),
  }))

  // The official-vs-clean block the API adds to every row (SPEC section 10.4).
  const model = buildEventModel(detail.laps, detail.entries.map((entry) => entry.driver), null)
  const best = new Map(
    buildBestLapRows(detail.entries, detail.laps, drivers, model).map((row) => [row.driver, row]),
  )
  for (const row of drivers) {
    const official = best.get(row.driver)
    if (official === undefined) continue
    row.official_best_ms = official.officialMs
    row.official_best_source = 'classification'
    row.official_best_lap_number = official.officialLapNumber
    row.official_best_lap_id =
      detail.laps.find(
        (lap) => lap.driver === row.driver && lap.lap_number === official.officialLapNumber,
      )?.id ?? null
    row.official_best_tags = official.officialTags
    row.official_best_is_joker = official.officialIsJoker
    row.best_delta_ms = official.deltaMs
  }
  const jokerInflated = drivers
    .filter((row) => row.official_best_is_joker)
    .map((row) => row.driver)

  return Promise.resolve({
    filter: {
      exclude_tags: filter.exclude_tags,
      mad_k: filter.mad_k,
      drop_missing: true,
      drop_first_lap: filter.drop_first_lap,
      drop_slow_outliers: filter.drop_slow_outliers,
      drop_fast_outliers: filter.drop_fast_outliers,
      min_laps: filter.min_laps,
    },
    drivers,
    official_best: {
      joker_inflated_drivers: jokerInflated,
      joker_inflated_count: jokerInflated.length,
      drivers_count: drivers.length,
      label: data.rankings.label ?? '',
      note: data.rankings.note ?? '',
    },
  })
}

export function demoComparison(
  sessionId: number,
  a: string,
  b: string,
  filter: LapFilterState,
): Promise<DriverComparison> {
  const detail = detailFor(sessionId)
  const known = new Set(detail.entries.map((entry) => entry.driver))
  for (const name of [a, b]) {
    if (!known.has(name)) throw new ApiError(404, `Неизвестный пилот: ${name}`)
  }
  return Promise.resolve(compareDrivers(lapsOf(detail, a), lapsOf(detail, b), a, b, filter))
}
