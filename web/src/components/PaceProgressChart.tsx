/**
 * Rolling-mean pace chart, drawn as plain SVG (no Recharts).
 *
 * One trailing moving average per driver over the laps `/stats` counted, so the
 * curve and the metrics table describe the same sample. A and B carry the two
 * accent hues, everyone else is the de-emphasis grey — the question this chart
 * answers is «как менялся мой темп», not «покажи шесть равноправных серий».
 *
 * Series are labelled at the end of their line instead of in a legend, so the
 * eye never travels; the labels are de-collided vertically before they render.
 */

import { useEffect, useMemo, useState } from 'react'

import type { DriverStats, LapRow } from '../api'
import { formatDuration, formatSignedDuration } from '../format'
import type { MetricDef } from '../metrics'
import { LAP_METRIC, readMetric, rollingMean } from '../metrics'
import type { ThemeMode } from '../theme'
import { CHART_CHROME, seriesColor } from '../theme'
import { Card, ViewToggle } from './Card'

export interface PaceProgressChartProps {
  laps: readonly LapRow[]
  /** `/stats` rows: the OLS slope of the trend lines comes from the backend. */
  rows: readonly DriverStats[]
  /** Whole lap or one sector; shared with the distribution tab. */
  metric?: MetricDef
  usedLaps: ReadonlyMap<string, ReadonlySet<number>>
  subject: string
  rival: string
  window: number
  onWindow: (window: number) => void
  /** Plot the deviation from each driver's own median instead of absolute time. */
  relative: boolean
  onRelative: (relative: boolean) => void
  mode: ThemeMode
  stale?: boolean
}

const WIDTH = 1340
const HEIGHT = 320
const PAD_LEFT = 54
const PAD_RIGHT = 94
const PAD_TOP = 16
const AXIS_Y = 282
/** Minimum vertical distance between two end-of-line labels, in viewBox units. */
const LABEL_GAP = 14

/**
 * Window ladder, shortest first. Which of these are offered depends on the race:
 * a 15-lap window over a 20-lap sprint yields three points — a straight line
 * that invites over-reading — while a 100-lap endurance run needs the long ones
 * to show anything but noise.
 */
const WINDOW_LADDER: readonly number[] = [3, 5, 7, 10, 15, 20, 30]

/** Fewest points a smoothed curve must have to be worth drawing. */
const MIN_CURVE_POINTS = 5

const VIEW_OPTIONS = [
  { value: 'abs', label: 'время круга' },
  { value: 'rel', label: 'к своей медиане' },
]

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b)
  const middle = (sorted.length - 1) / 2
  return sorted.length % 2 === 1
    ? sorted[middle]
    : (sorted[Math.floor(middle)] + sorted[Math.ceil(middle)]) / 2
}

export function PaceProgressChart({
  laps,
  rows,
  metric = LAP_METRIC,
  usedLaps,
  subject,
  rival,
  window,
  onWindow,
  relative,
  onRelative,
  mode,
  stale = false,
}: PaceProgressChartProps) {
  const [hoverLap, setHoverLap] = useState<number | null>(null)
  const chrome = CHART_CHROME[mode]
  const accent = chrome.accent
  const rivalColor = seriesColor(1, mode)

  // Longest stint of pace laps: the ceiling on a window that still says anything.
  const capacity = useMemo(() => {
    const byDriver = readMetric(laps, metric)
    let longest = 0
    for (const entry of byDriver.values()) {
      const used = usedLaps.get(entry.driver)
      const counted = entry.points.filter((point) =>
        used === undefined ? !point.excluded : used.has(point.lap),
      )
      longest = Math.max(longest, counted.length)
    }
    return longest
  }, [laps, usedLaps, metric])

  const windowOptions = useMemo(() => {
    const offered = WINDOW_LADDER.filter(
      (size) => size <= Math.max(WINDOW_LADDER[0], capacity - MIN_CURVE_POINTS + 1),
    )
    const usable = offered.length > 0 ? offered : [WINDOW_LADDER[0]]
    return usable.map((size) => ({ value: String(size), label: String(size) }))
  }, [capacity])

  // Moving from an endurance race to a sprint must not leave a 30-lap window on
  // a 20-lap stint.
  useEffect(() => {
    if (!windowOptions.some((option) => Number(option.value) === window)) {
      onWindow(Number(windowOptions[windowOptions.length - 1].value))
    }
  }, [windowOptions, window, onWindow])

  const series = useMemo(() => {
    const byDriver = readMetric(laps, metric)
    const lapCount = laps.reduce((max, lap) => Math.max(max, lap.lap_number), 1)
    const curves = [...byDriver.values()].map((entry) => {
      const used = usedLaps.get(entry.driver)
      const counted = entry.points.filter((point) =>
        used === undefined ? !point.excluded : used.has(point.lap),
      )
      const base = counted.length > 0 ? median(counted.map((point) => point.value)) : 0
      const curve = rollingMean(
        counted.map((point) => ({ ...point, excluded: false })),
        Math.min(window, Math.max(counted.length, 1)),
      )
      return {
        driver: entry.driver,
        base,
        // Raw counted laps stay available for the hover read-out: the curve is
        // an average, and the question «какой это был круг» needs the lap.
        raw: counted.map((point) => ({
          lap: point.lap,
          value: relative ? point.value - base : point.value,
        })),
        points: curve.map((point) => ({
          lap: point.lap,
          value: relative ? point.value - base : point.value,
        })),
      }
    })
    return { rows: curves.filter((row) => row.points.length > 1), lapCount }
  }, [laps, usedLaps, window, relative, metric])

  if (series.rows.length === 0) {
    return (
      <Card title="Скользящее среднее по ходу гонки" stale={stale}>
        <p className="empty">Недостаточно зачётных кругов для окна {window}.</p>
      </Card>
    )
  }

  const values = series.rows.flatMap((row) => row.points.map((point) => point.value))
  const lo = Math.min(...values) - 40
  const hi = Math.max(...values) + 40
  const x = (lap: number): number =>
    PAD_LEFT + ((lap - 1) / Math.max(1, series.lapCount - 1)) * (WIDTH - PAD_LEFT - PAD_RIGHT)
  const y = (value: number): number =>
    PAD_TOP + (1 - (value - lo) / (hi - lo)) * (AXIS_Y - PAD_TOP)

  const colourOf = (driver: string): string =>
    driver === subject ? accent : driver === rival ? rivalColor : chrome.deemphasis

  /**
   * Least-squares trend for A and B, drawn dashed.
   *
   * The slope is the backend's `degradation_ms_per_lap` — the browser must not
   * re-fit anything. An OLS line passes through the centroid of its sample, so
   * the mean lap number and the mean lap time pin the intercept exactly.
   */
  const trends = [subject, rival]
    .map((driver) => {
      const row = rows.find((item) => item.driver === driver)
      const slope = row?.degradation_ms_per_lap
      const meanMs = row?.mean_ms
      const used = row?.used_lap_numbers ?? []
      if (
        row === undefined ||
        slope === null ||
        slope === undefined ||
        meanMs === null ||
        meanMs === undefined ||
        used.length < 2
      ) {
        return null
      }
      const centre: number = meanMs
      const centreLap = used.reduce((sum, lap) => sum + lap, 0) / used.length
      const base = series.rows.find((item) => item.driver === driver)?.base ?? 0
      const at = (lap: number): number =>
        (relative ? centre - base : centre) + slope * (lap - centreLap)
      const from = Math.min(...used)
      const to = Math.max(...used)
      return { driver, colour: colourOf(driver), from, to, at, significant: (row.degradation_p_value ?? 1) < 0.05 }
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)

  // Nearest lap under the pointer, for the read-out below the chart.
  const hovered =
    hoverLap === null
      ? null
      : series.rows
          .map((row) => {
            const point = row.raw.reduce<{ lap: number; value: number } | null>(
              (best, candidate) =>
                best === null || Math.abs(candidate.lap - hoverLap) < Math.abs(best.lap - hoverLap)
                  ? candidate
                  : best,
              null,
            )
            return point === null ? null : { driver: row.driver, ...point }
          })
          .filter((item): item is { driver: string; lap: number; value: number } => item !== null)
          .sort((a, b) => a.value - b.value)

  // End labels stand in for the legend, so they must never overlap.
  const endLabels = series.rows
    .map((row) => {
      const last = row.points[row.points.length - 1]
      return { driver: row.driver, x: x(last.lap) + 9, y: y(last.value), colour: colourOf(row.driver) }
    })
    .sort((a, b) => a.y - b.y)
  for (let index = 1; index < endLabels.length; index += 1) {
    const gap = endLabels[index].y - endLabels[index - 1].y
    if (gap < LABEL_GAP) endLabels[index].y = endLabels[index - 1].y + LABEL_GAP
  }
  const overflow = endLabels[endLabels.length - 1].y - (AXIS_Y - 6)
  if (overflow > 0) {
    for (const label of endLabels) label.y -= overflow
    for (let index = 1; index < endLabels.length; index += 1) {
      const gap = endLabels[index].y - endLabels[index - 1].y
      if (gap < LABEL_GAP) endLabels[index].y = endLabels[index - 1].y + LABEL_GAP
    }
  }

  const ticks = [0, 1, 2, 3, 4].map((step) => lo + ((hi - lo) * step) / 4)
  const lapTicks: number[] = []
  for (let lap = 2; lap <= series.lapCount; lap += 2) lapTicks.push(lap)

  return (
    <Card
      title="Скользящее среднее по ходу гонки"
      caption={
        relative
          ? 'Ноль — своя медиана: пилоты сравнимы по форме, а не по абсолютному темпу.'
          : `Окно ${window} зачётных кругов, скользящее назад. Джокер и пит в среднее не входят.`
      }
      stale={stale}
      actions={
        <div className="chart-controls">
          <ViewToggle
            label="Окно скользящего среднего"
            value={String(window)}
            options={windowOptions}
            onChange={(value) => {
              onWindow(Number(value))
            }}
          />
          <ViewToggle
            label="Шкала графика"
            value={relative ? 'rel' : 'abs'}
            options={VIEW_OPTIONS}
            onChange={(value) => {
              onRelative(value === 'rel')
            }}
          />
        </div>
      }
    >
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width="100%" className="chart-frame" role="img">
        {ticks.map((value) => (
          <g key={value}>
            <line
              x1={PAD_LEFT}
              y1={y(value)}
              x2={WIDTH - PAD_RIGHT}
              y2={y(value)}
              stroke={chrome.grid}
            />
            <text x={PAD_LEFT - 8} y={y(value) + 3.5} textAnchor="end" fontSize="10.5" fill={chrome.muted}>
              {relative ? formatSignedDuration(value) : formatDuration(value)}
            </text>
          </g>
        ))}
        {relative && (
          <line
            x1={PAD_LEFT}
            y1={y(0)}
            x2={WIDTH - PAD_RIGHT}
            y2={y(0)}
            stroke={chrome.axis}
            strokeDasharray="4 4"
          />
        )}
        {series.rows.map((row) => {
          const hot = row.driver === subject || row.driver === rival
          return (
            <polyline
              key={row.driver}
              points={row.points.map((point) => `${x(point.lap)},${y(point.value)}`).join(' ')}
              fill="none"
              stroke={colourOf(row.driver)}
              strokeWidth={hot ? 2.4 : 1.3}
              strokeOpacity={hot ? 1 : 0.55}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          )
        })}
        {trends.map((trend) => (
          <line
            key={`trend-${trend.driver}`}
            x1={x(trend.from)}
            y1={y(trend.at(trend.from))}
            x2={x(trend.to)}
            y2={y(trend.at(trend.to))}
            stroke={trend.colour}
            strokeWidth={trend.significant ? 1.8 : 1.2}
            strokeDasharray="7 5"
            strokeOpacity={trend.significant ? 0.95 : 0.5}
          >
            <title>
              {`${trend.driver}: линейный тренд${trend.significant ? '' : ' (не значим, p ≥ 0.05)'}`}
            </title>
          </line>
        ))}
        {hoverLap !== null && (
          <line
            x1={x(hoverLap)}
            y1={PAD_TOP}
            x2={x(hoverLap)}
            y2={AXIS_Y}
            stroke={chrome.axis}
            strokeWidth={1}
          />
        )}
        {hoverLap !== null &&
          (hovered ?? []).map((point) => (
            <circle
              key={`hit-${point.driver}`}
              cx={x(point.lap)}
              cy={y(point.value)}
              r={point.driver === subject || point.driver === rival ? 4 : 2.6}
              fill={colourOf(point.driver)}
              stroke={chrome.surface}
              strokeWidth={1.5}
            />
          ))}
        <rect
          x={PAD_LEFT}
          y={PAD_TOP}
          width={WIDTH - PAD_LEFT - PAD_RIGHT}
          height={AXIS_Y - PAD_TOP}
          fill="transparent"
          onMouseLeave={() => {
            setHoverLap(null)
          }}
          onMouseMove={(event) => {
            // The SVG scales to the card, so map client px back to viewBox units.
            const box = event.currentTarget.getBoundingClientRect()
            const ratio = (event.clientX - box.left) / box.width
            const lap =
              1 +
              Math.round(
                ((ratio * (WIDTH - PAD_LEFT - PAD_RIGHT) + PAD_LEFT - PAD_LEFT) /
                  (WIDTH - PAD_LEFT - PAD_RIGHT)) *
                  Math.max(1, series.lapCount - 1),
              )
            setHoverLap(Math.min(series.lapCount, Math.max(1, lap)))
          }}
        />
        {endLabels.map((label) => (
          <text
            key={label.driver}
            x={label.x}
            y={label.y + 3.5}
            fontSize="11"
            fontWeight={label.driver === subject || label.driver === rival ? 650 : 400}
            fill={label.colour}
          >
            {label.driver}
          </text>
        ))}
        <line x1={PAD_LEFT} y1={AXIS_Y} x2={WIDTH - PAD_RIGHT} y2={AXIS_Y} stroke={chrome.axis} />
        {lapTicks.map((lap) => (
          <text key={lap} x={x(lap)} y={AXIS_Y + 18} textAnchor="middle" fontSize="10.5" fill={chrome.muted}>
            {lap}
          </text>
        ))}
        <text x={PAD_LEFT} y={HEIGHT - 6} fontSize="9.5" fill={chrome.muted}>
          круг →
        </text>
      </svg>
      <div className="chart-readout" role="status">
        {hovered === null || hovered.length === 0 ? (
          <span className="muted">
            Наведите курсор на график — покажу времена кругов в этой точке. Пунктиром — линейный
            тренд A и B: сплошной пунктир значим (p &lt; 0.05), бледный — нет.
          </span>
        ) : (
          <>
            <strong>круг {hoverLap}</strong>
            {hovered.map((point) => (
              <span key={point.driver}>
                <span
                  className="key-line"
                  style={{ backgroundColor: colourOf(point.driver) }}
                  aria-hidden="true"
                />
                <span className={point.driver === subject || point.driver === rival ? '' : 'muted'}>
                  {point.driver}
                </span>{' '}
                <b>{relative ? formatSignedDuration(point.value) : formatDuration(point.value)}</b>
              </span>
            ))}
          </>
        )}
      </div>
    </Card>
  )
}
