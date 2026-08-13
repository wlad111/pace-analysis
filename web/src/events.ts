/**
 * Joker and pit laps on the client (SPEC section 10).
 *
 * Every driver of this format must take the joker (a shortcut, roughly 1.9 s
 * faster) once and stop in the pits (roughly 13 s slower) once. Neither lap is
 * pace, so both are tagged and filtered out — and because the email's official
 * "Best lap" is the joker lap for five of the six drivers of the reference
 * race, the dashboard always shows the clean best lap next to it.
 *
 * Nothing here fetches: the model is built from the laps, the pace statistics
 * and the detector report the API already returned. The tags carried by the
 * laps are the source of truth for *what is in force*; the detector report only
 * adds the numbers behind a suggestion (ratio, delta, sector, confidence).
 */

import type { DetectedEvent, DriverStats, EntryRow, EventReport, LapRow, TagSource } from './api'
import { JOKER_TAG, PIT_TAG } from './api'

export type EventKind = 'joker' | 'pit'

export const EVENT_KINDS: readonly EventKind[] = [JOKER_TAG, PIT_TAG] as EventKind[]

export const EVENT_LABELS: Record<EventKind, string> = {
  joker: 'Джокер',
  pit: 'Пит-стоп',
}

/** One `lap_annotation` row, normalised. */
export interface LapAnnotationInfo {
  tag: string
  source: TagSource
  note: string | null
}

function isEventKind(value: string): value is EventKind {
  return value === JOKER_TAG || value === PIT_TAG
}

/**
 * Annotations of a lap with their origin. A tag that arrives as a bare string
 * counts as manual unless the payload lists it under `auto_tags`, mirroring
 * `karting.api.app.normalise_tags`: only the detector ever writes `auto`.
 */
export function annotationsOf(lap: LapRow): LapAnnotationInfo[] {
  const auto = new Set(lap.auto_tags ?? [])
  const result: LapAnnotationInfo[] = []
  const seen = new Set<string>()
  for (const ref of lap.tags) {
    const isText = typeof ref === 'string'
    const tag = isText ? ref : ref.tag
    if (!tag) continue
    const declared = isText ? null : (ref.source ?? null)
    const fallback: TagSource = auto.has(tag) ? 'auto' : 'manual'
    const source: TagSource = declared === 'auto' ? 'auto' : declared === 'manual' ? 'manual' : fallback
    const key = `${tag}:${source}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push({ tag, source, note: isText ? null : (ref.note ?? null) })
  }
  return result
}

/**
 * Tags that actually apply to a lap (SPEC section 10.3): one manual annotation
 * makes the whole automatic set of that lap irrelevant.
 */
export function effectiveTagsOf(lap: LapRow): string[] {
  if (lap.effective_tags !== undefined) return lap.effective_tags
  const annotations = annotationsOf(lap)
  const manual = annotations.filter((item) => item.source === 'manual')
  const chosen = manual.length > 0 ? manual : annotations
  return [...new Set(chosen.map((item) => item.tag))]
}

/** A joker or pit lap that is in force, with the detector's numbers if known. */
export interface LapEvent {
  kind: EventKind
  driver: string
  lapId: number
  lapNumber: number
  timeMs: number | null
  /** Who decided: the detector, or a human. */
  source: TagSource
  note: string | null
  detection: DetectedEvent | null
}

export interface DriverEventRow {
  driver: string
  joker: LapEvent | null
  pit: LapEvent | null
  /** Extra tagged laps beyond the first of a kind — the format allows one. */
  extra: LapEvent[]
  /**
   * Proposals that are not in force: a detection a human overrode, or — when no
   * pit stop was found at all — the slowest lap the detector offers instead
   * (SPEC section 10.2, `EventReport.pit_candidates`).
   */
  suggestions: DetectedEvent[]
  /** Every timed lap of the driver, for the manual-annotation controls. */
  laps: LapRow[]
}

export interface EventModel {
  rows: DriverEventRow[]
  events: LapEvent[]
  /** Lap id -> event, for the lap-time chart markers. */
  byLapId: Map<number, LapEvent>
  jokers: number
  pits: number
  manual: number
  driversWithoutJoker: string[]
  driversWithoutPit: string[]
  /** True once the API answered `/events`; false means "tags only". */
  detectorAvailable: boolean
  warnings: string[]
}

function detectionOf(
  report: EventReport | null,
  driver: string,
  kind: EventKind,
  lapNumber: number,
): DetectedEvent | null {
  if (report === null) return null
  return (
    report.events.find(
      (event) =>
        event.driver === driver && event.kind === kind && event.lap_number === lapNumber,
    ) ?? null
  )
}

/**
 * Builds the joker/pit model of a session.
 *
 * `drivers` fixes the row order (the classification), so the panel never
 * reshuffles when a tag changes.
 */
export function buildEventModel(
  laps: readonly LapRow[],
  drivers: readonly string[],
  report: EventReport | null,
): EventModel {
  const byDriver = new Map<string, LapRow[]>()
  for (const lap of laps) {
    const bucket = byDriver.get(lap.driver)
    if (bucket === undefined) byDriver.set(lap.driver, [lap])
    else bucket.push(lap)
  }
  for (const bucket of byDriver.values()) bucket.sort((a, b) => a.lap_number - b.lap_number)

  const order = [...drivers, ...[...byDriver.keys()].filter((name) => !drivers.includes(name))]
  // Detections and pit proposals are offered to the reader through the same
  // channel: both are "the detector thinks this lap, decide for yourself".
  const proposed: DetectedEvent[] =
    report === null ? [] : [...report.events, ...(report.pit_candidates ?? [])]
  const rows: DriverEventRow[] = []
  const all: LapEvent[] = []
  const byLapId = new Map<number, LapEvent>()
  let manual = 0

  for (const driver of order) {
    const driverLaps = byDriver.get(driver) ?? []
    const found: LapEvent[] = []
    for (const lap of driverLaps) {
      const annotations = annotationsOf(lap)
      for (const tag of effectiveTagsOf(lap)) {
        if (!isEventKind(tag)) continue
        const annotation =
          annotations.find((item) => item.tag === tag && item.source === 'manual') ??
          annotations.find((item) => item.tag === tag)
        const event: LapEvent = {
          kind: tag,
          driver,
          lapId: lap.id,
          lapNumber: lap.lap_number,
          timeMs: lap.time_ms,
          source: annotation?.source ?? 'manual',
          note: annotation?.note ?? null,
          detection: detectionOf(report, driver, tag, lap.lap_number),
        }
        found.push(event)
        all.push(event)
        byLapId.set(lap.id, event)
        if (event.source === 'manual') manual += 1
      }
    }
    const jokers = found.filter((event) => event.kind === JOKER_TAG)
    const pits = found.filter((event) => event.kind === PIT_TAG)
    const applied = new Set(found.map((event) => `${event.kind}:${event.lapNumber}`))
    rows.push({
      driver,
      joker: jokers[0] ?? null,
      pit: pits[0] ?? null,
      extra: [...jokers.slice(1), ...pits.slice(1)],
      suggestions: proposed.filter(
        (event) => event.driver === driver && !applied.has(`${event.kind}:${event.lap_number}`),
      ),
      laps: driverLaps,
    })
  }

  return {
    rows,
    events: all,
    byLapId,
    jokers: all.filter((event) => event.kind === JOKER_TAG).length,
    pits: all.filter((event) => event.kind === PIT_TAG).length,
    manual,
    driversWithoutJoker: rows.filter((row) => row.joker === null).map((row) => row.driver),
    driversWithoutPit: rows.filter((row) => row.pit === null).map((row) => row.driver),
    detectorAvailable: report !== null,
    warnings: report?.warnings ?? [],
  }
}

/** One driver in the "official vs clean best lap" block. */
export interface BestLapRow {
  driver: string
  position: number | null
  /** The email's "Best lap" column. */
  officialMs: number | null
  officialLapNumber: number | null
  officialIsJoker: boolean
  officialTags: string[]
  /** Fastest lap that survives the current filter. */
  cleanMs: number | null
  cleanLapNumber: number | null
  /** `clean - official`, i.e. how much the official number flatters the driver. */
  deltaMs: number | null
  officialRank: number | null
  cleanRank: number | null
  /** Places gained when the field is ranked on clean pace instead. */
  rankShift: number | null
}

function rankByTime(rows: readonly { driver: string; value: number | null }[]): Map<string, number> {
  const ranked = rows
    .filter((row): row is { driver: string; value: number } => row.value !== null)
    .sort((a, b) => a.value - b.value || a.driver.localeCompare(b.driver))
  return new Map(ranked.map((row, index) => [row.driver, index + 1]))
}

/**
 * The central table of the product: the official best lap, the clean best lap
 * and the delta, per driver, plus the two rankings they produce.
 *
 * The server sends the official block with `/stats` when it is new enough; the
 * classification and the lap tags carry the same information, so the rows are
 * complete either way.
 */
export function buildBestLapRows(
  entries: readonly EntryRow[],
  laps: readonly LapRow[],
  stats: readonly DriverStats[],
  model: EventModel,
): BestLapRow[] {
  const statsByDriver = new Map(stats.map((row) => [row.driver, row]))
  const lapsByDriver = new Map<string, LapRow[]>()
  for (const lap of laps) {
    const bucket = lapsByDriver.get(lap.driver)
    if (bucket === undefined) lapsByDriver.set(lap.driver, [lap])
    else bucket.push(lap)
  }
  const jokerLap = new Map(
    model.events
      .filter((event) => event.kind === JOKER_TAG)
      .map((event) => [event.driver, event.lapNumber]),
  )

  const rows: BestLapRow[] = entries.map((entry) => {
    const stat = statsByDriver.get(entry.driver)
    const driverLaps = lapsByDriver.get(entry.driver) ?? []
    const officialMs = stat?.official_best_ms ?? entry.best_lap_ms ?? null
    const officialLap =
      driverLaps.find((lap) => lap.lap_number === stat?.official_best_lap_number) ??
      driverLaps.find((lap) => lap.is_best && lap.time_ms === officialMs) ??
      driverLaps.find((lap) => lap.time_ms !== null && lap.time_ms === officialMs) ??
      driverLaps.find((lap) => lap.is_best) ??
      null
    const officialTags =
      stat?.official_best_tags ?? (officialLap === null ? [] : effectiveTagsOf(officialLap))
    const cleanMs = stat?.best_ms ?? null
    const cleanLap =
      cleanMs === null ? null : (driverLaps.find((lap) => lap.time_ms === cleanMs) ?? null)
    return {
      driver: entry.driver,
      position: entry.position,
      officialMs,
      officialLapNumber: officialLap?.lap_number ?? stat?.official_best_lap_number ?? null,
      officialIsJoker:
        stat?.official_best_is_joker ??
        (officialTags.includes(JOKER_TAG) ||
          (officialLap !== null && jokerLap.get(entry.driver) === officialLap.lap_number)),
      officialTags,
      cleanMs,
      cleanLapNumber: cleanLap?.lap_number ?? null,
      deltaMs:
        stat?.best_delta_ms ?? (cleanMs === null || officialMs === null ? null : cleanMs - officialMs),
      officialRank: null,
      cleanRank: null,
      rankShift: null,
    }
  })

  const officialRanks = rankByTime(rows.map((row) => ({ driver: row.driver, value: row.officialMs })))
  const cleanRanks = rankByTime(rows.map((row) => ({ driver: row.driver, value: row.cleanMs })))
  for (const row of rows) {
    row.officialRank = officialRanks.get(row.driver) ?? null
    row.cleanRank = cleanRanks.get(row.driver) ?? null
    row.rankShift =
      row.officialRank === null || row.cleanRank === null ? null : row.officialRank - row.cleanRank
  }
  return rows
}
