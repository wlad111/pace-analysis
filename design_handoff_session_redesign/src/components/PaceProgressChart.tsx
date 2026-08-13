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

import { useMemo } from 'react'

import type { LapRow } from '../api'
import { formatDuration, formatSignedDuration } from '../format'
import { LAP_METRIC, readMetric, rollingMean } from '../metrics'
import type { ThemeMode } from '../theme'
import { CHART_CHROME, seriesColor } from '../theme'
import { Card, ViewToggle } from './Card'

export interface PaceProgressChartProps {
  laps: readonly LapRow[]
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

// ViewToggle is generic over string, so the options carry string values.
const WINDOW_OPTIONS = [
  { value: '3', label: '3' },
  { value: '5', label: '5' },
  { value: '7', label: '7' },
]

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
  const chrome = CHART_CHROME[mode]
  const accent = chrome.accent
  const rivalColor = seriesColor(1, mode)

  const series = useMemo(() => {
    const byDriver = readMetric(laps, LAP_METRIC)
    const lapCount = laps.reduce((max, lap) => Math.max(max, lap.lap_number), 1)
    const rows = [...byDriver.values()].map((entry) => {
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
        points: curve.map((point) => ({
          lap: point.lap,
          value: relative ? point.value - base : point.value,
        })),
      }
    })
    return { rows: rows.filter((row) => row.points.length > 1), lapCount }
  }, [laps, usedLaps, window, relative])

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
            options={WINDOW_OPTIONS}
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
    </Card>
  )
}
