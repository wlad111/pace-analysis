/**
 * Metrics a chart can be drawn against: the full lap, or one sector of it.
 *
 * Sector times only reach us for the recipient of an e-mail (SPEC section 1.5),
 * so a sector metric usually covers fewer drivers than the session has. Every
 * component that offers a sector view has to say so out loud rather than draw a
 * one-driver chart as if it were the whole race — hence `metricCoverage`.
 */

import type { LapRow } from './api'
import { effectiveTagsOf } from './events'

export interface MetricDef {
  /** `lap`, or `s1`, `s2`, … for sectors. */
  id: string
  /** Full name for captions: «Круг», «Сектор 1». */
  label: string
  /** Compact name for the segmented control: «Круг», «S1». */
  short: string
  /** Sector position, or `null` for the whole lap. */
  sector: number | null
  value: (lap: LapRow) => number | null
}

export const LAP_METRIC: MetricDef = {
  id: 'lap',
  label: 'Круг целиком',
  short: 'Круг',
  sector: null,
  value: (lap) => lap.time_ms,
}

export function sectorMetric(index: number): MetricDef {
  return {
    id: `s${index + 1}`,
    label: `Сектор ${index + 1}`,
    short: `S${index + 1}`,
    sector: index,
    value: (lap) => lap.sectors[index] ?? null,
  }
}

/** How many sector columns the session carries at all. */
export function sectorCount(laps: readonly LapRow[]): number {
  let count = 0
  for (const lap of laps) {
    for (let index = 0; index < lap.sectors.length; index += 1) {
      if (lap.sectors[index] !== null && index + 1 > count) count = index + 1
    }
  }
  return count
}

/** The lap metric first, then one entry per sector present in the data. */
export function availableMetrics(laps: readonly LapRow[]): MetricDef[] {
  const metrics = [LAP_METRIC]
  for (let index = 0; index < sectorCount(laps); index += 1) metrics.push(sectorMetric(index))
  return metrics
}

export interface MetricCoverage {
  /** Drivers with at least one value for this metric. */
  covered: string[]
  /** Drivers in the session, whether or not they have values. */
  total: number
  complete: boolean
}

export function metricCoverage(laps: readonly LapRow[], metric: MetricDef): MetricCoverage {
  const drivers = new Set<string>()
  const covered = new Set<string>()
  for (const lap of laps) {
    drivers.add(lap.driver)
    if (metric.value(lap) !== null) covered.add(lap.driver)
  }
  return {
    covered: [...covered],
    total: drivers.size,
    complete: covered.size === drivers.size,
  }
}

/** Values of one metric for one driver, ordered by lap number. */
export interface DriverValues {
  driver: string
  points: { lap: number; value: number; excluded: boolean }[]
}

/**
 * Reads a metric out of the payload per driver. `excluded` marks laps carrying
 * one of `excludedTags` (the joker and the pit lap by default): they are kept in
 * the result so a chart can show where they happened, but no pace figure should
 * be computed from them.
 */
export function readMetric(
  laps: readonly LapRow[],
  metric: MetricDef,
  excludedTags: ReadonlySet<string> = new Set(['joker', 'pit']),
): Map<string, DriverValues> {
  const byDriver = new Map<string, DriverValues>()
  for (const lap of laps) {
    const value = metric.value(lap)
    if (value === null) continue
    let bucket = byDriver.get(lap.driver)
    if (bucket === undefined) {
      bucket = { driver: lap.driver, points: [] }
      byDriver.set(lap.driver, bucket)
    }
    bucket.points.push({
      lap: lap.lap_number,
      value,
      excluded: effectiveTagsOf(lap).some((tag) => excludedTags.has(tag)),
    })
  }
  for (const bucket of byDriver.values()) bucket.points.sort((a, b) => a.lap - b.lap)
  return byDriver
}

/**
 * Trailing moving average over the laps that count as pace.
 *
 * Trailing rather than centred: the point above lap N answers «каким был темп на
 * последних N кругах», which is the question a race engineer actually asks, and
 * it never borrows information from laps that had not happened yet. Excluded
 * laps (joker, pit) are skipped entirely instead of being averaged in — one pit
 * stop would otherwise drag the curve for `window` laps.
 */
export function rollingMean(
  points: readonly { lap: number; value: number; excluded: boolean }[],
  window: number,
): { lap: number; value: number }[] {
  const usable = points.filter((point) => !point.excluded)
  const result: { lap: number; value: number }[] = []
  let sum = 0
  for (let index = 0; index < usable.length; index += 1) {
    sum += usable[index].value
    if (index >= window) sum -= usable[index - window].value
    if (index >= window - 1) {
      result.push({ lap: usable[index].lap, value: sum / window })
    }
  }
  return result
}


/**
 * Laps the backend actually counted, per driver.
 *
 * The charts must summarise exactly the sample the pace table quotes, or the
 * two disagree on screen. `/stats` reports `used_lap_numbers` per driver, so we
 * follow it rather than re-deriving the filter in the browser. Before the stats
 * arrive (or for a driver missing from them) the caller falls back to the tag
 * test, which is weaker but never counts a joker or a pit lap as pace.
 */
export function usedLapsByDriver(
  stats: readonly { driver: string; used_lap_numbers?: number[] | null }[] | null,
): Map<string, ReadonlySet<number>> {
  const result = new Map<string, ReadonlySet<number>>()
  for (const row of stats ?? []) {
    if (Array.isArray(row.used_lap_numbers)) result.set(row.driver, new Set(row.used_lap_numbers))
  }
  return result
}
