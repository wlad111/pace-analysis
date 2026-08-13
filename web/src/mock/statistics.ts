/**
 * Demo-only statistics engine.
 *
 * It mirrors `karting.stats` (SPEC section 8.3) closely enough that the offline
 * dashboard behaves like the real one: the same filter semantics, the same
 * metric names, the same tests. It is NOT a replacement for the backend —
 * SciPy is the source of truth. Where SciPy uses exact distributions this
 * module uses the standard asymptotic approximations (noted in the caveats the
 * comparison returns), and it is only ever reached when the API is offline.
 */

import type {
  DriverComparison,
  LapFilterState,
  LapFlag,
  PaceStats,
  TestResult,
} from '../api'
import { mad, mean, median, quantile, robustScale, std, trimmedMean } from '../descriptive'

export interface LapPoint {
  lap_number: number
  time_ms: number | null
  sectors: (number | null)[]
  tags: string[]
}

// --------------------------------------------------------------------------
// Distributions (Numerical-Recipes style; enough precision for a demo)
// --------------------------------------------------------------------------

// Copied verbatim from Numerical Recipes: the trailing digits are past what a
// double can hold and are kept only so the constants match the published table.
const LANCZOS = [
  // oxlint-disable-next-line no-loss-of-precision
  76.18009172947146, -86.50532032941677, 24.01409824083091, -1.231739572450155,
  0.1208650973866179e-2, -0.5395239384953e-5,
]

function logGamma(x: number): number {
  let y = x
  const tmp = x + 5.5 - (x + 0.5) * Math.log(x + 5.5)
  let ser = 1.000000000190015
  for (const coefficient of LANCZOS) {
    y += 1
    ser += coefficient / y
  }
  // oxlint-disable-next-line no-loss-of-precision
  return -tmp + Math.log((2.5066282746310005 * ser) / x)
}

function betacf(a: number, b: number, x: number): number {
  const tiny = 1e-30
  const qab = a + b
  const qap = a + 1
  const qam = a - 1
  let c = 1
  let d = 1 - (qab * x) / qap
  if (Math.abs(d) < tiny) d = tiny
  d = 1 / d
  let h = d
  for (let m = 1; m <= 300; m += 1) {
    const m2 = 2 * m
    let aa = (m * (b - m) * x) / ((qam + m2) * (a + m2))
    d = 1 + aa * d
    if (Math.abs(d) < tiny) d = tiny
    c = 1 + aa / c
    if (Math.abs(c) < tiny) c = tiny
    d = 1 / d
    h *= d * c
    aa = (-(a + m) * (qab + m) * x) / ((a + m2) * (qap + m2))
    d = 1 + aa * d
    if (Math.abs(d) < tiny) d = tiny
    c = 1 + aa / c
    if (Math.abs(c) < tiny) c = tiny
    d = 1 / d
    const del = d * c
    h *= del
    if (Math.abs(del - 1) < 3e-12) break
  }
  return h
}

/** Regularised incomplete beta function I_x(a, b). */
function betainc(a: number, b: number, x: number): number {
  if (x <= 0) return 0
  if (x >= 1) return 1
  const front = Math.exp(
    logGamma(a + b) - logGamma(a) - logGamma(b) + a * Math.log(x) + b * Math.log(1 - x),
  )
  return x < (a + 1) / (a + b + 2)
    ? (front * betacf(a, b, x)) / a
    : 1 - (front * betacf(b, a, 1 - x)) / b
}

/** Two-sided p-value of Student's t with `df` degrees of freedom. */
export function studentTTwoSided(t: number, df: number): number | null {
  if (!Number.isFinite(t) || df <= 0) return null
  return betainc(df / 2, 0.5, df / (df + t * t))
}

/** Upper-tail p-value of the F distribution. */
export function fUpperTail(f: number, df1: number, df2: number): number | null {
  if (!Number.isFinite(f) || f < 0 || df1 <= 0 || df2 <= 0) return null
  return betainc(df2 / 2, df1 / 2, df2 / (df2 + df1 * f))
}

function erf(x: number): number {
  // Abramowitz & Stegun 7.1.26, |error| < 1.5e-7.
  const sign = x < 0 ? -1 : 1
  const z = Math.abs(x)
  const t = 1 / (1 + 0.3275911 * z)
  const y =
    1 -
    ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t +
      0.254829592) *
      t *
      Math.exp(-z * z)
  return sign * y
}

export function normalTwoSided(z: number): number {
  return 2 * (1 - 0.5 * (1 + erf(Math.abs(z) / Math.SQRT2)))
}

/** Two-sided t quantile for confidence intervals, found by bisection. */
function tQuantile(p: number, df: number): number {
  let lo = 0
  let hi = 200
  for (let i = 0; i < 200; i += 1) {
    const mid = (lo + hi) / 2
    const tail = studentTTwoSided(mid, df) ?? 1
    if (1 - tail < p) lo = mid
    else hi = mid
  }
  return (lo + hi) / 2
}

/** Deterministic PRNG so the bootstrap is reproducible, like the seeded API. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// --------------------------------------------------------------------------
// Lap classification
// --------------------------------------------------------------------------

interface Classified {
  flags: LapFlag[]
  used: LapPoint[]
}

/**
 * Applies the lap filter. Robust centre and spread are computed on the laps
 * that survive the deterministic filters (missing / first lap / tags), so an
 * out-lap or a tagged lap never moves the outlier threshold.
 */
export function classifyLaps(laps: readonly LapPoint[], filter: LapFilterState): Classified {
  const excludeTags = new Set(filter.exclude_tags)
  const flags: LapFlag[] = []
  const candidates: LapPoint[] = []

  for (const lap of laps) {
    if (lap.time_ms === null) {
      flags.push({ lap_number: lap.lap_number, used: false, reason: 'missing', suspicious_fast: false })
      continue
    }
    if (filter.drop_first_lap && lap.lap_number === 1) {
      flags.push({ lap_number: lap.lap_number, used: false, reason: 'first_lap', suspicious_fast: false })
      continue
    }
    const tag = lap.tags.find((value) => excludeTags.has(value))
    if (tag !== undefined) {
      flags.push({ lap_number: lap.lap_number, used: false, reason: `tag:${tag}`, suspicious_fast: false })
      continue
    }
    candidates.push(lap)
  }

  const times = candidates.map((lap) => lap.time_ms as number)
  const centre = times.length > 0 ? median(times) : 0
  const spread = times.length > 0 ? robustScale(times) : 0
  const used: LapPoint[] = []

  for (const lap of candidates) {
    const time = lap.time_ms as number
    const isSlow = spread > 0 && time > centre + filter.mad_k * spread
    const isFast = spread > 0 && time < centre - filter.mad_k * spread
    if (isSlow && filter.drop_slow_outliers) {
      flags.push({ lap_number: lap.lap_number, used: false, reason: 'slow_outlier', suspicious_fast: false })
      continue
    }
    if (isFast && filter.drop_fast_outliers) {
      flags.push({ lap_number: lap.lap_number, used: false, reason: 'fast_outlier', suspicious_fast: true })
      continue
    }
    flags.push({ lap_number: lap.lap_number, used: true, reason: null, suspicious_fast: isFast })
    used.push(lap)
  }

  flags.sort((a, b) => a.lap_number - b.lap_number)
  used.sort((a, b) => a.lap_number - b.lap_number)
  return { flags, used }
}

function emptyStats(nLaps: number, flags: LapFlag[], nUsed: number): PaceStats {
  return {
    n_laps: nLaps,
    n_used: nUsed,
    best_ms: null,
    median_ms: null,
    mean_ms: null,
    trimmed_mean_ms: null,
    std_ms: null,
    iqr_ms: null,
    mad_ms: null,
    cv: null,
    consistency: null,
    theoretical_best_ms: null,
    degradation_ms_per_lap: null,
    degradation_p_value: null,
    used_lap_numbers: [],
    excluded: flags.filter((flag) => !flag.used),
  }
}

/** Sum of the best sector times, over every lap that carries a full set. */
function theoreticalBest(laps: readonly LapPoint[]): number | null {
  const width = laps.reduce((max, lap) => Math.max(max, lap.sectors.length), 0)
  if (width === 0) return null
  const bests: (number | null)[] = new Array<number | null>(width).fill(null)
  for (const lap of laps) {
    if (lap.sectors.length !== width) continue
    lap.sectors.forEach((sector, index) => {
      if (sector === null) return
      const current = bests[index]
      if (current === null || sector < current) bests[index] = sector
    })
  }
  if (bests.some((value) => value === null)) return null
  return Math.round(bests.reduce<number>((total, value) => total + (value ?? 0), 0))
}

interface Slope {
  slope: number
  pValue: number | null
}

/** OLS of lap time on lap number, with the slope's two-sided t test. */
function regressionSlope(xs: readonly number[], ys: readonly number[]): Slope | null {
  const n = xs.length
  if (n < 3) return null
  const mx = mean(xs)
  const my = mean(ys)
  let sxx = 0
  let sxy = 0
  for (let i = 0; i < n; i += 1) {
    sxx += (xs[i] - mx) ** 2
    sxy += (xs[i] - mx) * (ys[i] - my)
  }
  if (sxx === 0) return null
  const slope = sxy / sxx
  const intercept = my - slope * mx
  let sse = 0
  for (let i = 0; i < n; i += 1) sse += (ys[i] - (intercept + slope * xs[i])) ** 2
  const df = n - 2
  const se = Math.sqrt(sse / df / sxx)
  if (!Number.isFinite(se) || se === 0) return { slope, pValue: null }
  return { slope, pValue: studentTTwoSided(slope / se, df) }
}

export function paceStats(laps: readonly LapPoint[], filter: LapFilterState): PaceStats {
  const { flags, used } = classifyLaps(laps, filter)
  const times = used.map((lap) => lap.time_ms as number)
  if (times.length === 0 || times.length < filter.min_laps) {
    return emptyStats(laps.length, flags, times.length)
  }
  const med = median(times)
  const avg = mean(times)
  const deviation = std(times)
  const regression = regressionSlope(used.map((lap) => lap.lap_number), times)
  return {
    n_laps: laps.length,
    n_used: times.length,
    best_ms: Math.min(...times),
    median_ms: med,
    mean_ms: avg,
    trimmed_mean_ms: trimmedMean(times),
    std_ms: deviation,
    iqr_ms: quantile(times, 0.75) - quantile(times, 0.25),
    mad_ms: mad(times),
    cv: deviation === null ? null : deviation / avg,
    consistency: deviation === null ? null : deviation / med,
    theoretical_best_ms: theoreticalBest(laps),
    degradation_ms_per_lap: regression?.slope ?? null,
    degradation_p_value: regression?.pValue ?? null,
    used_lap_numbers: used.map((lap) => lap.lap_number),
    excluded: flags.filter((flag) => !flag.used),
  }
}

// --------------------------------------------------------------------------
// Two-driver comparison
// --------------------------------------------------------------------------

function welch(a: readonly number[], b: readonly number[]): TestResult {
  const n1 = a.length
  const n2 = b.length
  const v1 = (std(a) ?? 0) ** 2
  const v2 = (std(b) ?? 0) ** 2
  const se = Math.sqrt(v1 / n1 + v2 / n2)
  const diff = mean(a) - mean(b)
  if (se === 0 || n1 < 2 || n2 < 2) {
    return {
      name: 'Тест Уэлча',
      statistic: null,
      p_value: null,
      ci_low: null,
      ci_high: null,
      effect_size: null,
      effect_name: "Hedges' g",
      interpretation: 'Кругов не хватает, чтобы оценить дисперсию.',
    }
  }
  const df =
    (v1 / n1 + v2 / n2) ** 2 / (v1 ** 2 / (n1 ** 2 * (n1 - 1)) + v2 ** 2 / (n2 ** 2 * (n2 - 1)))
  const t = diff / se
  const p = studentTTwoSided(t, df)
  const crit = tQuantile(0.95, df)
  const pooled = Math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
  const d = pooled === 0 ? 0 : diff / pooled
  const g = d * (1 - 3 / (4 * (n1 + n2) - 9))
  return {
    name: 'Тест Уэлча',
    statistic: t,
    p_value: p,
    ci_low: diff - crit * se,
    ci_high: diff + crit * se,
    effect_size: g,
    effect_name: "Hedges' g",
    interpretation:
      p !== null && p < 0.05
        ? 'Средние времена круга различаются сильнее, чем объяснил бы один только шум выборки.'
        : 'Заметной разницы средних времён круга на уровне 5% нет.',
  }
}

function mannWhitney(a: readonly number[], b: readonly number[]): TestResult {
  const n1 = a.length
  const n2 = b.length
  const pooled = [...a.map((v) => ({ v, group: 0 })), ...b.map((v) => ({ v, group: 1 }))].sort(
    (x, y) => x.v - y.v,
  )
  const ranks = new Array<number>(pooled.length)
  let tieCorrection = 0
  let i = 0
  while (i < pooled.length) {
    let j = i
    while (j + 1 < pooled.length && pooled[j + 1].v === pooled[i].v) j += 1
    const rank = (i + j + 2) / 2
    for (let k = i; k <= j; k += 1) ranks[k] = rank
    const groupSize = j - i + 1
    tieCorrection += groupSize ** 3 - groupSize
    i = j + 1
  }
  let rankSumA = 0
  pooled.forEach((item, index) => {
    if (item.group === 0) rankSumA += ranks[index]
  })
  const u1 = rankSumA - (n1 * (n1 + 1)) / 2
  const n = n1 + n2
  const muU = (n1 * n2) / 2
  const sigma = Math.sqrt(
    ((n1 * n2) / 12) * (n + 1 - tieCorrection / (n * (n - 1))),
  )
  const z = sigma === 0 ? 0 : (u1 - muU - Math.sign(u1 - muU) * 0.5) / sigma
  const p = sigma === 0 ? null : normalTwoSided(z)
  // Cliff's delta is a linear function of U and needs no extra pass.
  const delta = (2 * u1) / (n1 * n2) - 1
  const magnitude = Math.abs(delta)
  const label =
    magnitude < 0.147 ? 'negligible' : magnitude < 0.33 ? 'small' : magnitude < 0.474 ? 'medium' : 'large'
  return {
    name: 'Mann-Whitney U',
    statistic: u1,
    p_value: p,
    ci_low: null,
    ci_high: null,
    effect_size: delta,
    effect_name: "Cliff's delta",
    interpretation: `Rank-based comparison, ${label} effect (normal approximation with tie correction).`,
  }
}

function levene(a: readonly number[], b: readonly number[]): TestResult {
  // Brown-Forsythe variant: absolute deviations from the group median.
  const groups = [a, b].map((group) => {
    const centre = median(group)
    return group.map((value) => Math.abs(value - centre))
  })
  const all = groups.flat()
  const grand = mean(all)
  const n = all.length
  const k = groups.length
  let between = 0
  let within = 0
  for (const group of groups) {
    const groupMean = mean(group)
    between += group.length * (groupMean - grand) ** 2
    for (const value of group) within += (value - groupMean) ** 2
  }
  if (within === 0) {
    return {
      name: 'Levene (Brown-Forsythe)',
      statistic: null,
      p_value: null,
      ci_low: null,
      ci_high: null,
      effect_size: null,
      effect_name: null,
      interpretation: 'Вырожденный разброс; тест не определён.',
    }
  }
  const w = ((n - k) * between) / ((k - 1) * within)
  const p = fUpperTail(w, k - 1, n - k)
  return {
    name: 'Levene (Brown-Forsythe)',
    statistic: w,
    p_value: p,
    ci_low: null,
    ci_high: null,
    effect_size: null,
    effect_name: null,
    interpretation:
      p !== null && p < 0.05
        ? 'Lap-time spread differs: one driver is measurably less consistent.'
        : 'Заметной разницы в разбросе времён круга нет.',
  }
}

function bootstrapMedianDiff(
  a: readonly number[],
  b: readonly number[],
  nBoot: number,
  seed: number,
): TestResult {
  const random = mulberry32(seed)
  const diffs = new Float64Array(nBoot)
  const sampleA = new Array<number>(a.length)
  const sampleB = new Array<number>(b.length)
  for (let i = 0; i < nBoot; i += 1) {
    for (let j = 0; j < a.length; j += 1) sampleA[j] = a[Math.floor(random() * a.length)]
    for (let j = 0; j < b.length; j += 1) sampleB[j] = b[Math.floor(random() * b.length)]
    diffs[i] = median(sampleA) - median(sampleB)
  }
  const values = Array.from(diffs)
  const observed = median(a) - median(b)
  const low = quantile(values, 0.025)
  const high = quantile(values, 0.975)
  return {
    name: 'Бутстрэп разности медиан',
    statistic: observed,
    p_value: null,
    ci_low: low,
    ci_high: high,
    effect_size: null,
    effect_name: null,
    interpretation:
      low <= 0 && high >= 0
        ? 'The 95% interval contains zero: the median gap is not resolved by this sample.'
        : 'The 95% interval excludes zero: the median gap survives resampling.',
  }
}

export function compareDrivers(
  lapsA: readonly LapPoint[],
  lapsB: readonly LapPoint[],
  nameA: string,
  nameB: string,
  filter: LapFilterState,
  nBoot = 10000,
  seed = 12345,
): DriverComparison {
  const statsA = paceStats(lapsA, filter)
  const statsB = paceStats(lapsB, filter)
  const usedA = classifyLaps(lapsA, filter).used.map((lap) => lap.time_ms as number)
  const usedB = classifyLaps(lapsB, filter).used.map((lap) => lap.time_ms as number)

  const caveats = [
    'Круги одной гонки не являются независимыми наблюдениями: трафик, топливо, резина и трасса ' +
      'temperature drift, and slipstream all correlate consecutive laps. Every p-value below ' +
      'assumes independence it does not have, so read the effect sizes and intervals first.',
    'A single race is one sample of one day. Nothing here generalises to "who is faster" ' +
      'without more sessions.',
    'Демо-режим: эти числа посчитаны в браузере по асимптотическим приближениям. ' +
      'The API computes them with SciPy (exact tests where applicable) and is the source of truth.',
  ]
  if (usedA.length < 8 || usedB.length < 8) {
    caveats.push(
      `Small sample: ${usedA.length} clean laps for ${nameA} and ${usedB.length} for ${nameB}. ` +
        'На такой выборке тесты улавливают только очень крупные различия.',
    )
  }

  if (usedA.length === 0 || usedB.length === 0) {
    return {
      driver_a: nameA,
      driver_b: nameB,
      stats_a: statsA,
      stats_b: statsB,
      n_a: usedA.length,
      n_b: usedB.length,
      mean_diff_ms: null,
      median_diff_ms: null,
      tests: [],
      caveats: [...caveats, 'У одного из пилотов нет чистых кругов при текущем фильтре.'],
    }
  }

  return {
    driver_a: nameA,
    driver_b: nameB,
    stats_a: statsA,
    stats_b: statsB,
    n_a: usedA.length,
    n_b: usedB.length,
    mean_diff_ms: mean(usedA) - mean(usedB),
    median_diff_ms: median(usedA) - median(usedB),
    tests: [
      welch(usedA, usedB),
      mannWhitney(usedA, usedB),
      levene(usedA, usedB),
      bootstrapMedianDiff(usedA, usedB, nBoot, seed),
    ],
    caveats,
  }
}
