/**
 * Descriptive primitives shared by the charts and by the offline demo engine.
 * Nothing here talks to the API; every input is a plain array of milliseconds.
 */

export function sortedCopy(values: readonly number[]): number[] {
  return [...values].sort((a, b) => a - b)
}

export function mean(values: readonly number[]): number {
  let total = 0
  for (const value of values) total += value
  return total / values.length
}

/** Linear-interpolation quantile, matching numpy's default method. */
export function quantile(values: readonly number[], q: number): number {
  const xs = sortedCopy(values)
  if (xs.length === 1) return xs[0]
  const h = (xs.length - 1) * q
  const lo = Math.floor(h)
  const hi = Math.min(lo + 1, xs.length - 1)
  return xs[lo] + (h - lo) * (xs[hi] - xs[lo])
}

export function median(values: readonly number[]): number {
  return quantile(values, 0.5)
}

/** Sample standard deviation (ddof = 1); `null` for fewer than two points. */
export function std(values: readonly number[]): number | null {
  if (values.length < 2) return null
  const m = mean(values)
  let acc = 0
  for (const value of values) acc += (value - m) ** 2
  return Math.sqrt(acc / (values.length - 1))
}

/** Median absolute deviation (unscaled). */
export function mad(values: readonly number[]): number {
  const centre = median(values)
  return median(values.map((value) => Math.abs(value - centre)))
}

/** Below this many values nothing can be trimmed without gutting the sample. */
export const MIN_TRIM_VALUES = 5

/**
 * Symmetric trimmed mean, cutting `max(1, floor(n * proportion))` from each
 * tail, or `null` when the sample is too small to trim at all. Mirrors
 * `karting.stats.pace._trimmed_mean`: a "10% trimmed mean" that trims nothing
 * is just the mean wearing a different label.
 */
export function trimmedMean(values: readonly number[], proportion = 0.1): number | null {
  const xs = sortedCopy(values)
  if (xs.length < MIN_TRIM_VALUES) return null
  const cut = Math.max(1, Math.floor(xs.length * proportion))
  if (xs.length - 2 * cut < 1) return null
  return mean(xs.slice(cut, xs.length - cut))
}

/** `1.4826 * MAD` — the MAD rescaled to a normal-consistent sigma. */
export function scaledMad(values: readonly number[]): number {
  return 1.4826 * mad(values)
}

/**
 * Robust sigma with the same fallback chain as `karting.stats.robust_scale`:
 * scaled MAD, then `IQR / 1.349`, then `1.2533 * mean|x - median|`. Without it
 * a sample where half the values are identical would report a spread of 0 and
 * silently switch outlier detection off, keeping an arbitrarily gross lap.
 * `0` means every value really is identical.
 */
export function robustScale(values: readonly number[]): number {
  if (values.length === 0) return 0
  const byMad = scaledMad(values)
  if (byMad > 0) return byMad
  const iqr = quantile(values, 0.75) - quantile(values, 0.25)
  if (iqr > 0) return iqr * 0.7413
  const centre = median(values)
  const meanAd = mean(values.map((value) => Math.abs(value - centre)))
  return meanAd > 0 ? meanAd * 1.2533 : 0
}

export interface RobustBounds {
  centre: number
  spread: number
  /** median + k * scaled MAD — above this a lap is a slow outlier. */
  ceiling: number
  /** median - k * scaled MAD — below this a lap is suspiciously fast. */
  floor: number
}

export function robustBounds(values: readonly number[], k: number): RobustBounds | null {
  if (values.length === 0) return null
  const centre = median(values)
  const spread = robustScale(values)
  return {
    centre,
    spread,
    ceiling: spread > 0 ? centre + k * spread : Number.POSITIVE_INFINITY,
    floor: spread > 0 ? centre - k * spread : Number.NEGATIVE_INFINITY,
  }
}

export interface FiveNumber {
  min: number
  q1: number
  median: number
  q3: number
  max: number
  iqr: number
  mean: number
  /** Tukey whiskers: the extreme values still inside 1.5 * IQR. */
  whiskerLow: number
  whiskerHigh: number
  outliers: number[]
}

export function fiveNumberSummary(values: readonly number[]): FiveNumber | null {
  if (values.length === 0) return null
  const xs = sortedCopy(values)
  const q1 = quantile(xs, 0.25)
  const q3 = quantile(xs, 0.75)
  const iqr = q3 - q1
  const lowLimit = q1 - 1.5 * iqr
  const highLimit = q3 + 1.5 * iqr
  const inner = xs.filter((value) => value >= lowLimit && value <= highLimit)
  return {
    min: xs[0],
    q1,
    median: quantile(xs, 0.5),
    q3,
    max: xs[xs.length - 1],
    iqr,
    mean: mean(xs),
    whiskerLow: inner.length > 0 ? inner[0] : xs[0],
    whiskerHigh: inner.length > 0 ? inner[inner.length - 1] : xs[xs.length - 1],
    outliers: xs.filter((value) => value < lowLimit || value > highLimit),
  }
}

/** Rounds a raw step up to a readable 250 / 500 / 1000 ms grid. */
export function niceStep(raw: number): number {
  const candidates = [50, 100, 250, 500, 1000, 2000, 5000, 10_000, 30_000, 60_000]
  for (const candidate of candidates) {
    if (raw <= candidate) return candidate
  }
  return candidates[candidates.length - 1]
}

/** Axis ticks on a clean grid inside `[lo, hi]`. */
export function axisTicks(lo: number, hi: number, target = 5): number[] {
  const step = niceStep((hi - lo) / target)
  const first = Math.ceil(lo / step) * step
  const ticks: number[] = []
  for (let value = first; value <= hi + 1e-6; value += step) ticks.push(Math.round(value))
  return ticks
}
