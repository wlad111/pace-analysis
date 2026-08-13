#!/usr/bin/env node
/**
 * Regenerates `src/mock/session.json` from the hand-verified fixture
 * `tests/fixtures/final_a_expected.json`.
 *
 * The fixture stores durations as the literal strings printed in the email;
 * the API contract (SPEC section 2) stores integer milliseconds, so every
 * duration is converted here. The output is the exact JSON shape the
 * dashboard expects from `GET /api/sessions`, `GET /api/sessions/{id}` and
 * `GET /api/tags`; the demo engine (`src/mock/demo.ts`) derives `/stats` and
 * `/compare` from it.
 *
 * Usage: npm run build:mock
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FIXTURE = resolve(HERE, '../../tests/fixtures/final_a_expected.json')
const OUTPUT = resolve(HERE, '../src/mock/session.json')

const SESSION_ID = 1

/**
 * Parses a duration exactly like `karting.parsing.timeparse.parse_duration`.
 * @param {string | null | undefined} text
 * @returns {number | null}
 */
function parseDuration(text) {
  if (text === null || text === undefined) return null
  const cleaned = String(text).replace(/ |​/g, ' ').trim()
  if (cleaned === '' || cleaned === '-' || cleaned === '--') return null
  const normalised = cleaned.replace(/'/g, ':').replace(/,/g, '.')
  const match = /^(?:(\d+):)?(?:(\d+):)?(\d+)(?:\.(\d{1,3}))?$/.exec(normalised)
  if (match === null) throw new Error(`unparsable duration: ${text}`)
  const [, first, second, secondsPart, fractionPart] = match
  let hours = 0
  let minutes = 0
  if (first !== undefined && second !== undefined) {
    hours = Number(first)
    minutes = Number(second)
  } else if (first !== undefined) {
    minutes = Number(first)
  }
  const seconds = Number(secondsPart)
  // Normalise by digit count, never by a blind * 1000: "28.8" is 28800 ms.
  const fraction = fractionPart === undefined ? 0 : Number(fractionPart.padEnd(3, '0'))
  return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction
}

const fixture = JSON.parse(readFileSync(FIXTURE, 'utf8'))

const classification = fixture.classification
const driverOrder = classification.map((row) => row.driver)

const entries = classification.map((row) => ({
  driver: row.driver,
  position: row.position,
  kart: row.kart,
  laps_count: row.laps,
  gap_ms: parseDuration(row.gap),
  gap_laps: null,
  best_lap_ms: parseDuration(row.best_lap),
}))

/** Sector times of the recipient, keyed by lap number. */
const sectorsByLap = new Map()
for (const row of fixture.recipient_sector_table.rows) {
  sectorsByLap.set(row.lap, row.sectors.map(parseDuration))
}
const sectorDriver = fixture.recipient_sector_table.driver

const laps = []
let lapId = 0
for (const driver of driverOrder) {
  const times = fixture.laps[driver]
  const bestLapNumber = fixture.best_lap_number[driver]
  times.forEach((text, index) => {
    const lapNumber = index + 1
    lapId += 1
    laps.push({
      id: lapId,
      session_id: SESSION_ID,
      driver,
      lap_number: lapNumber,
      time_ms: parseDuration(text),
      sectors: driver === sectorDriver ? (sectorsByLap.get(lapNumber) ?? []) : [],
      is_best: lapNumber === bestLapNumber,
      // Same four views of the annotations as `karting.api.app.normalise_lap`:
      // every row with its source, the two origins apart, and what is in force.
      annotations: [],
      tags: [],
      manual_tags: [],
      auto_tags: [],
      effective_tags: [],
      manually_annotated: false,
    })
  })
}

// --------------------------------------------------------------------------
// Joker / pit detection (SPEC section 10.2)
// --------------------------------------------------------------------------
//
// A faithful port of `karting/stats/events.py`: this script must run with node
// alone (no Python, no database), and the offline demo would be misleading if
// its ratios, sectors, confidences and notes disagreed with the live API. The
// constants and the wording below are the ones in that module; the expectation
// block at the end of the section fails the build if the two ever drift apart
// on the reference race.

const EVENT_CONFIG = {
  pit_ratio: 1.25,
  joker_ratio: 0.97,
  one_per_driver: true,
  require_single_sector: true,
  skip_first_lap: true,
}

const MIN_BASELINE_LAPS = 3
const MIN_SECTOR_SAMPLES = 3
const SECTOR_ANOMALY_SHARE = 0.5
const CONFIDENCE_BASE = 0.15
const CONFIDENCE_MARGIN_WEIGHT = 0.5
const CONFIDENCE_SEPARATION_WEIGHT = 0.25
const SECTOR_CONFIRMED_BONUS = 0.08
const SECTOR_UNCONFIRMED_PENALTY = 0.25
const SECTOR_CONFIRMED = 'confirmed'
const SECTOR_UNCONFIRMED = 'unconfirmed'
const SECTOR_UNAVAILABLE = 'unavailable'

/** Lap numbers of the reference race, hand-verified. If detection disagrees, the mock is wrong. */
const EXPECTED = {
  pit: { KOLYA11: 17, WLAD111: 19, TWG: 3, DENISENKO: 5, PHREEMAN: 18, 'ИГОРЬ53': 14 },
  joker: { KOLYA11: 19, WLAD111: 3, TWG: 14, DENISENKO: 15, PHREEMAN: 5 },
}

/**
 * @param {number[]} values
 * @returns {number}
 */
function median(values) {
  const xs = [...values].sort((a, b) => a - b)
  const middle = (xs.length - 1) / 2
  return (xs[Math.floor(middle)] + xs[Math.ceil(middle)]) / 2
}

/** Milliseconds as `28.058`, like `karting.stats.events._seconds`. */
function seconds(value) {
  return (value / 1000).toFixed(3)
}

/** Milliseconds as `+12.284`, like `karting.stats.events._signed_seconds`. */
function signedSeconds(value) {
  return `${value < 0 ? '-' : '+'}${Math.abs(value / 1000).toFixed(3)}`
}

function clamp01(value) {
  return value < 0 ? 0 : value > 1 ? 1 : value
}

/**
 * Which event a lap time looks like against `baseline`.
 * @returns {'joker' | 'pit' | null}
 */
function candidateKind(timeMs, baseline) {
  if (timeMs >= EVENT_CONFIG.pit_ratio * baseline) return 'pit'
  if (timeMs <= EVENT_CONFIG.joker_ratio * baseline) return 'joker'
  return null
}

/**
 * Robust baseline in two passes: a rough median, then the median of the laps
 * the first pass did not consider candidates, so the events cannot drag the
 * reference they are measured against.
 * @param {number[]} values
 * @returns {number}
 */
function baselineOf(values) {
  const rough = median(values)
  if (rough <= 0) return rough
  const remaining = values.filter((value) => candidateKind(value, rough) === null)
  return remaining.length > 0 ? median(remaining) : rough
}

/**
 * Try to pin the lap-time anomaly on a single sector: exactly one sector must
 * move at least `SECTOR_ANOMALY_SHARE` of the whole lap deviation in the
 * expected direction, and no other sector may move that much either way.
 * @returns {{index: number | null, note: string, status: string}}
 */
function localiseInSector(lap, kind, lapDelta, baselineLaps) {
  const sectors = lap.sectors
  if (sectors.length === 0 || sectors.some((value) => value === null)) {
    return { index: null, note: 'no sector data for this lap', status: SECTOR_UNAVAILABLE }
  }
  if (sectors.length < 2) {
    return {
      index: null,
      note: 'a single sector cannot localise anything',
      status: SECTOR_UNAVAILABLE,
    }
  }
  const medians = sectors.map((_, index) => {
    const values = baselineLaps
      .map((item) => item.sectors[index])
      .filter((value) => typeof value === 'number')
    return values.length >= MIN_SECTOR_SAMPLES ? median(values) : null
  })
  if (medians.some((value) => value === null)) {
    return {
      index: null,
      note: 'not enough sector data to localise the anomaly',
      status: SECTOR_UNAVAILABLE,
    }
  }
  const scale = Math.abs(lapDelta)
  if (scale <= 0) {
    return {
      index: null,
      note: 'lap time equals the baseline, nothing to localise',
      status: SECTOR_UNAVAILABLE,
    }
  }
  const direction = kind === 'pit' ? 1 : -1
  const deltas = sectors.map((value, index) => value - medians[index])
  const shares = deltas.map((delta) => (direction * delta) / scale)
  const anomalous = shares.flatMap((share, index) => (share >= SECTOR_ANOMALY_SHARE ? [index] : []))
  const noisy = shares.flatMap((share, index) =>
    Math.abs(share) >= SECTOR_ANOMALY_SHARE ? [index] : [],
  )
  if (anomalous.length === 1 && noisy.length === 1 && noisy[0] === anomalous[0]) {
    const index = anomalous[0]
    return {
      index,
      note:
        `anomaly confined to sector S${index + 1} (${signedSeconds(deltas[index])} s vs its ` +
        `median, ${Math.round(shares[index] * 100)}% of the lap deviation)`,
      status: SECTOR_CONFIRMED,
    }
  }
  const detail = deltas.map((delta, index) => `S${index + 1} ${signedSeconds(delta)}`).join(', ')
  return {
    index: null,
    note: `anomaly not confined to one sector (${detail})`,
    status: SECTOR_UNCONFIRMED,
  }
}

/**
 * How safe the call is: how far past its threshold the candidate sits, plus how
 * wide the empty band between it and the most extreme ordinary lap is.
 * @returns {number}
 */
function confidenceOf(ratio, kind, otherRatios) {
  let scale
  let excess
  let gap
  if (kind === 'pit') {
    scale = Math.max(EVENT_CONFIG.pit_ratio - 1, 1e-3)
    excess = ratio - EVENT_CONFIG.pit_ratio
    gap = ratio - (otherRatios.length > 0 ? Math.max(...otherRatios) : ratio)
  } else {
    scale = Math.max(1 - EVENT_CONFIG.joker_ratio, 1e-3)
    excess = EVENT_CONFIG.joker_ratio - ratio
    gap = (otherRatios.length > 0 ? Math.min(...otherRatios) : ratio) - ratio
  }
  return (
    CONFIDENCE_BASE +
    CONFIDENCE_MARGIN_WEIGHT * clamp01(excess / scale) +
    CONFIDENCE_SEPARATION_WEIGHT * clamp01(gap / scale)
  )
}

/** @returns {{driver: string, lap_number: number, kind: string, ratio: number, delta_ms: number, sector_index: number | null, confidence: number, note: string}} */
function makeEvent(driver, lap, kind, baseline, confidence, sector) {
  const lapDelta = lap.time_ms - baseline
  return {
    driver,
    lap_number: lap.lap_number,
    kind,
    ratio: lap.time_ms / baseline,
    delta_ms: Math.round(lapDelta),
    sector_index: sector.index,
    confidence: Math.round(clamp01(confidence) * 10000) / 10000,
    note:
      `${kind}: ${seconds(lap.time_ms)} vs baseline ${seconds(baseline)} ` +
      `(${signedSeconds(lapDelta)} s, ratio ${(lap.time_ms / baseline).toFixed(3)}); ${sector.note}`,
  }
}

const events = []
const pitCandidates = []
const driversWithoutJoker = []
const driversWithoutPit = []
const driversWithMultiple = []
const detectionWarnings = []

for (const driver of driverOrder) {
  const own = laps.filter((lap) => lap.driver === driver && lap.time_ms !== null)
  const firstNumber = Math.min(...laps.filter((lap) => lap.driver === driver).map((lap) => lap.lap_number))
  const usable = own.filter((lap) => !(EVENT_CONFIG.skip_first_lap && lap.lap_number === firstNumber))
  if (usable.length < MIN_BASELINE_LAPS) {
    detectionWarnings.push(
      `${driver}: only ${usable.length} timed lap(s) available, at least ${MIN_BASELINE_LAPS} ` +
        'are needed for a baseline; detection skipped',
    )
    driversWithoutJoker.push(driver)
    driversWithoutPit.push(driver)
    continue
  }

  const baseline = baselineOf(usable.map((lap) => lap.time_ms))
  const candidates = { joker: [], pit: [] }
  for (const lap of usable) {
    const kind = candidateKind(lap.time_ms, baseline)
    if (kind !== null) candidates[kind].push(lap)
  }
  const candidateNumbers = new Set(
    [...candidates.joker, ...candidates.pit].map((lap) => lap.lap_number),
  )
  const baselineLaps = usable.filter((lap) => !candidateNumbers.has(lap.lap_number))

  for (const kind of ['joker', 'pit']) {
    const group = candidates[kind]
    if (group.length === 0) continue
    if (group.length > 1 && !driversWithMultiple.includes(driver)) driversWithMultiple.push(driver)
    const groupNumbers = new Set(group.map((lap) => lap.lap_number))
    // Most extreme wins; ties go to the earlier lap.
    const chosen = group.reduce((best, lap) =>
      kind === 'pit'
        ? lap.time_ms > best.time_ms
          ? lap
          : best
        : lap.time_ms < best.time_ms
          ? lap
          : best,
    )
    for (const lap of group) {
      if (lap.lap_number === chosen.lap_number) continue
      detectionWarnings.push(
        `${driver}: lap ${lap.lap_number} also looks like a ${kind} ` +
          `(ratio ${(lap.time_ms / baseline).toFixed(3)}) but was not selected; ` +
          `lap ${chosen.lap_number} is the more extreme candidate`,
      )
    }
    const otherRatios = usable
      .filter((lap) => !groupNumbers.has(lap.lap_number))
      .map((lap) => lap.time_ms / baseline)
    const ratio = chosen.time_ms / baseline
    const sector = localiseInSector(chosen, kind, chosen.time_ms - baseline, baselineLaps)
    let confidence = confidenceOf(ratio, kind, otherRatios)
    if (sector.status === SECTOR_CONFIRMED) confidence += SECTOR_CONFIRMED_BONUS
    else if (sector.status === SECTOR_UNCONFIRMED && EVENT_CONFIG.require_single_sector)
      confidence -= SECTOR_UNCONFIRMED_PENALTY
    const event = makeEvent(driver, chosen, kind, baseline, confidence, sector)
    events.push(event)
    const lap = laps.find((item) => item.id === chosen.id)
    const annotation = { tag: kind, note: `auto-detected ${kind}`, created_at: null, source: 'auto' }
    lap.annotations.push(annotation)
    lap.tags.push(annotation)
    lap.auto_tags.push(kind)
    lap.effective_tags.push(kind)
  }

  if (candidates.joker.length === 0) driversWithoutJoker.push(driver)
  if (candidates.pit.length === 0) {
    // The pit stop is mandatory, so a missing one is a problem to resolve and
    // must come with the lap to confirm (SPEC section 10.2).
    driversWithoutPit.push(driver)
    const slowest = usable.reduce((worst, lap) => (lap.time_ms > worst.time_ms ? lap : worst))
    const lapDelta = slowest.time_ms - baseline
    const sector = localiseInSector(slowest, 'pit', lapDelta, baselineLaps)
    const ratio = slowest.time_ms / baseline
    pitCandidates.push({
      driver,
      lap_number: slowest.lap_number,
      kind: 'pit',
      ratio,
      delta_ms: Math.round(lapDelta),
      sector_index: sector.index,
      confidence: 0,
      note:
        `proposed pit stop (below the detection threshold): slowest lap, ` +
        `${seconds(slowest.time_ms)} vs baseline ${seconds(baseline)} ` +
        `(${signedSeconds(lapDelta)} s, ratio ${ratio.toFixed(3)}); ${sector.note}`,
    })
    detectionWarnings.push(
      `${driver}: no pit stop detected, but every driver must pit exactly once -- this is a data ` +
        `or detection problem, not a race fact. The slowest lap is ${slowest.lap_number} at ratio ` +
        `${ratio.toFixed(3)} (${signedSeconds(lapDelta)} s vs the baseline); confirm it by hand ` +
        'if that was the pit stop',
    )
  }
}

for (const event of events) {
  event.time_ms = laps.find(
    (lap) => lap.driver === event.driver && lap.lap_number === event.lap_number,
  ).time_ms
  event.lap_id = laps.find(
    (lap) => lap.driver === event.driver && lap.lap_number === event.lap_number,
  ).id
  event.applied = true
  event.overridden_by_manual = false
}

// The reference race is hand-verified: six pit stops and five jokers, with
// ИГОРЬ53 missing a joker. If the generator disagrees, the mock is wrong.
for (const kind of ['pit', 'joker']) {
  const found = Object.fromEntries(
    events.filter((event) => event.kind === kind).map((event) => [event.driver, event.lap_number]),
  )
  const expected = EXPECTED[kind]
  const same =
    Object.keys(found).length === Object.keys(expected).length &&
    Object.entries(expected).every(([driver, lap]) => found[driver] === lap)
  if (!same) {
    throw new Error(
      `detected ${kind} laps do not match the reference race: ` +
        `${JSON.stringify(found)} != ${JSON.stringify(expected)}`,
    )
  }
}

const eventReport = {
  session_id: SESSION_ID,
  config: EVENT_CONFIG,
  events,
  drivers_without_joker: driversWithoutJoker,
  drivers_without_pit: driversWithoutPit,
  drivers_with_multiple: driversWithMultiple,
  pit_candidates: pitCandidates,
  warnings: detectionWarnings,
  counts: {
    drivers: driverOrder.length,
    joker: events.filter((event) => event.kind === 'joker').length,
    pit: events.filter((event) => event.kind === 'pit').length,
  },
  persisted: true,
  complete:
    driversWithoutJoker.length === 0 &&
    driversWithoutPit.length === 0 &&
    driversWithMultiple.length === 0,
}

const rankings = {
  weekly_best: fixture.weekly_best.map((row) => ({
    rank: row.rank,
    driver: row.driver,
    best_lap_ms: parseDuration(row.best_lap),
    category: fixture.session.category,
  })),
  track_record: fixture.track_records.map((row) => ({
    rank: row.rank,
    driver: row.driver,
    best_lap_ms: parseDuration(row.best_lap),
    category: fixture.session.category,
  })),
  official_best_based: true,
  joker_inflated: true,
  label: 'official, joker-inflated',
  // Verbatim from `karting.api.schemas.JOKER_INFLATED_NOTE`, so the offline
  // demo warns about the leaderboards in exactly the same words as the API.
  note:
    'Built from the official best laps of the email. In this format every driver runs one ' +
    'mandatory joker lap (~1.9 s faster), so these times are most likely joker laps and not ' +
    'representative pace. Compare them with the clean best lap of the pace table.',
}

const session = {
  id: SESSION_ID,
  name: fixture.session.name,
  code: fixture.session.code,
  started_at: fixture.session.started_at,
  track: fixture.session.track,
  category: fixture.session.category,
  tz_name: null,
}

const club = {
  name: fixture.club.name,
  external_id: fixture.club.external_id,
  website: fixture.club.website,
  email: fixture.provenance.from_email,
}

const payload = {
  _comment:
    'Generated by web/scripts/build-mock.mjs from tests/fixtures/final_a_expected.json. ' +
    'Durations are integer milliseconds. Used as offline demo data by src/mock/demo.ts.',
  sessions: [
    {
      id: SESSION_ID,
      name: session.name,
      code: session.code,
      started_at: session.started_at,
      track: session.track,
      category: session.category,
      club: club.name,
      drivers_count: driverOrder.length,
      laps_count: laps.length,
    },
  ],
  session: { session, club, entries, laps },
  tags: [
    { value: 'penalty', label: 'Penalty' },
    { value: 'joker', label: 'Joker' },
    { value: 'pit', label: 'Pit' },
    { value: 'boost', label: 'Boost' },
    { value: 'traffic', label: 'Traffic' },
    { value: 'incident', label: 'Incident' },
    { value: 'outlier', label: 'Outlier' },
    { value: 'invalid', label: 'Invalid' },
    { value: 'clean', label: 'Clean' },
  ],
  events: eventReport,
  rankings,
}

writeFileSync(OUTPUT, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
process.stdout.write(
  `wrote ${OUTPUT}: ${driverOrder.length} drivers, ${laps.length} laps, ` +
    `${eventReport.counts.joker} jokers, ${eventReport.counts.pit} pit stops\n`,
)
