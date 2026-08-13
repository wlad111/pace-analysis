import { useMemo, useState } from 'react'

import { axisTicks, fiveNumberSummary, mean, std } from '../descriptive'
import type { FiveNumber } from '../descriptive'
import { EVENT_LABELS } from '../events'
import { formatClock, formatDuration } from '../format'
import type { ThemeMode } from '../theme'
import { CHART_CHROME } from '../theme'

/**
 * Which summary the box draws.
 *
 * `quantile` is the classic Tukey box: it says nothing about the shape of the
 * distribution and survives a 40 s incident lap untouched. `moments` draws mean
 * ± SD, which is what the pace table and every t-test on the page actually
 * operate on — the two disagree exactly when the sample is skewed, and being
 * able to flip between them is the point.
 */
export type BoxStatMode = 'quantile' | 'moments'

/** One lap (or sector) drawn as a dot next to its box. */
export interface BoxPoint {
  lap: number
  value: number
  tags: string[]
  /**
   * Whether the lap counts towards the summary.
   *
   * The box is built from counted laps only — the joker, the pit stop and slow
   * outliers are events, not pace, and a single 64 s pit lap in a 98-lap stint
   * inflates the SD by a factor of twenty. Excluded laps are still drawn, in
   * grey: hiding them would misrepresent the race just as badly.
   */
  counted: boolean
}

export interface BoxSeries {
  name: string
  color: string
  points: BoxPoint[]
}

interface Moments {
  mean: number
  sd: number
  /** Box edges: mean ± SD. Whiskers: mean ± 2 SD. */
  low: number
  high: number
  whiskerLow: number
  whiskerHigh: number
}

interface Prepared {
  series: BoxSeries
  summary: FiveNumber
  moments: Moments
  /** How many laps the summary was built from. */
  counted: number
}

type Hover =
  | { kind: 'box'; driver: string }
  | { kind: 'point'; driver: string; point: BoxPoint }
  | null

export interface BoxPlotProps {
  series: readonly BoxSeries[]
  statMode: BoxStatMode
  mode: ThemeMode
  /** Plot height in px; the head-to-head view passes a taller value. */
  height?: number
  /** Horizontal room per box; wider when there are only two of them. */
  slot?: number
  /** Extra line under the readout, e.g. the metric being plotted. */
  emptyText?: string
}

const PAD_TOP = 14
const PAD_BOTTOM = 40
const PAD_LEFT = 78
const PAD_RIGHT = 12

function moments(values: readonly number[]): Moments {
  const centre = mean(values)
  const spread = std(values) ?? 0
  return {
    mean: centre,
    sd: spread,
    low: centre - spread,
    high: centre + spread,
    whiskerLow: centre - 2 * spread,
    whiskerHigh: centre + 2 * spread,
  }
}

/**
 * Interactive box plot with the raw laps drawn beside each box (a strip plot).
 *
 * Shared by the distribution card and the head-to-head panel so the two never
 * drift apart: the same marks, the same colours, the same readout.
 */
export function BoxPlot({
  series,
  statMode,
  mode,
  height = 340,
  slot = 92,
  emptyText = 'Нет данных для этой метрики.',
}: BoxPlotProps) {
  const [hover, setHover] = useState<Hover>(null)
  const [pinned, setPinned] = useState<string | null>(null)
  const chrome = CHART_CHROME[mode]

  const boxes = useMemo<Prepared[]>(() => {
    const result: Prepared[] = []
    for (const item of series) {
      const values = item.points.filter((point) => point.counted).map((point) => point.value)
      if (values.length === 0) continue
      const summary = fiveNumberSummary(values)
      if (summary === null) continue
      result.push({ series: item, summary, moments: moments(values), counted: values.length })
    }
    return result
  }, [series])

  const scale = useMemo(() => {
    if (boxes.length === 0) return null
    // The axis follows the whiskers, not the extremes: one 40 s incident lap
    // would otherwise squash every box into a single line.
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    for (const box of boxes) {
      const shape = statMode === 'quantile' ? box.summary : box.moments
      lo = Math.min(lo, shape.whiskerLow)
      hi = Math.max(hi, shape.whiskerHigh)
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null
    const pad = Math.max(200, (hi - lo) * 0.12)
    return { lo: Math.floor(lo - pad), hi: Math.ceil(hi + pad) }
  }, [boxes, statMode])

  const width = Math.max(360, PAD_LEFT + PAD_RIGHT + boxes.length * slot)
  const plotHeight = height - PAD_TOP - PAD_BOTTOM
  const half = Math.min(28, slot / 2 - 12)
  const y = (value: number): number => {
    if (scale === null) return PAD_TOP
    const clamped = Math.min(Math.max(value, scale.lo), scale.hi)
    return PAD_TOP + plotHeight * (1 - (clamped - scale.lo) / (scale.hi - scale.lo))
  }
  const ticks = scale === null ? [] : axisTicks(scale.lo, scale.hi, 5)

  const focused = pinned ?? (hover === null ? null : hover.driver)
  const hoveredBox: Prepared | null =
    hover === null ? null : (boxes.find((box) => box.series.name === hover.driver) ?? null)

  if (boxes.length === 0 || scale === null) return <p className="empty">{emptyText}</p>

  return (
    <>
      <div className="chart-frame chart-hoverable" style={{ overflowX: 'auto' }}>
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={
            statMode === 'quantile'
              ? 'Ящик с усами: медиана и межквартильный размах'
              : 'Ящик с усами: среднее и стандартное отклонение'
          }
          onMouseLeave={() => {
            setHover(null)
          }}
        >
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={PAD_LEFT - 6}
                x2={width - PAD_RIGHT}
                y1={y(tick)}
                y2={y(tick)}
                stroke={chrome.grid}
                strokeWidth={1}
              />
              <text
                x={PAD_LEFT - 10}
                y={y(tick) + 4}
                textAnchor="end"
                fill={chrome.muted}
                fontSize={11}
              >
                {formatClock(tick)}
              </text>
            </g>
          ))}
          {boxes.map((box, index) => {
            const centre = PAD_LEFT + slot / 2 + index * slot
            const item = box.series
            const shape = statMode === 'quantile' ? box.summary : box.moments
            const middle = statMode === 'quantile' ? box.summary.median : box.moments.mean
            const boxLow = statMode === 'quantile' ? box.summary.q1 : box.moments.low
            const boxHigh = statMode === 'quantile' ? box.summary.q3 : box.moments.high
            const faded = focused !== null && focused !== item.name
            const isPinned = pinned === item.name
            return (
              <g key={item.name} opacity={faded ? 0.22 : 1} style={{ transition: 'opacity 120ms' }}>
                {/* Full-height hit area: pointing anywhere in the column works. */}
                <rect
                  x={centre - slot / 2}
                  y={PAD_TOP}
                  width={slot}
                  height={plotHeight}
                  fill={isPinned ? item.color : 'transparent'}
                  fillOpacity={isPinned ? 0.06 : 0}
                  onMouseEnter={() => {
                    setHover({ kind: 'box', driver: item.name })
                  }}
                  onClick={() => {
                    setPinned((current) => (current === item.name ? null : item.name))
                  }}
                  style={{ cursor: 'pointer' }}
                />
                <line
                  x1={centre}
                  x2={centre}
                  y1={y(shape.whiskerHigh)}
                  y2={y(shape.whiskerLow)}
                  stroke={item.color}
                  strokeWidth={1.5}
                  pointerEvents="none"
                />
                {[shape.whiskerHigh, shape.whiskerLow].map((cap) => (
                  <line
                    key={cap}
                    x1={centre - half / 2}
                    x2={centre + half / 2}
                    y1={y(cap)}
                    y2={y(cap)}
                    stroke={item.color}
                    strokeWidth={1.5}
                    pointerEvents="none"
                  />
                ))}
                <rect
                  x={centre - half}
                  y={y(boxHigh)}
                  width={half * 2}
                  height={Math.max(1, y(boxLow) - y(boxHigh))}
                  fill={item.color}
                  fillOpacity={0.18}
                  stroke={item.color}
                  strokeWidth={1.5}
                  rx={2}
                  pointerEvents="none"
                />
                <line
                  x1={centre - half}
                  x2={centre + half}
                  y1={y(middle)}
                  y2={y(middle)}
                  stroke={item.color}
                  strokeWidth={3}
                  pointerEvents="none"
                />
                {item.points.map((point) => {
                  const outside = point.value > shape.whiskerHigh || point.value < shape.whiskerLow
                  const active =
                    hover?.kind === 'point' &&
                    hover.driver === item.name &&
                    hover.point.lap === point.lap
                  return (
                    <circle
                      key={point.lap}
                      cx={centre + half + 9}
                      cy={y(point.value)}
                      r={active ? 4.5 : point.counted ? 2.2 : 3}
                      fill={!point.counted || outside ? chrome.textSecondary : item.color}
                      fillOpacity={active ? 1 : point.counted ? 0.75 : 0.4}
                      stroke={active ? chrome.surface : 'none'}
                      strokeWidth={active ? 1.5 : 0}
                      onMouseEnter={() => {
                        setHover({ kind: 'point', driver: item.name, point })
                      }}
                      style={{ cursor: 'pointer' }}
                    />
                  )
                })}
                <text
                  x={centre}
                  y={height - 22}
                  textAnchor="middle"
                  fill={isPinned ? item.color : chrome.textSecondary}
                  fontSize={11}
                  fontWeight={isPinned ? 600 : 400}
                  pointerEvents="none"
                >
                  {item.name.length > 11 ? `${item.name.slice(0, 10)}…` : item.name}
                </text>
                <text
                  x={centre}
                  y={height - 8}
                  textAnchor="middle"
                  fill={chrome.muted}
                  fontSize={10}
                  pointerEvents="none"
                >
                  {formatDuration(middle)}
                </text>
              </g>
            )
          })}
        </svg>
      </div>

      <div className="chart-readout" role="status">
        {hover === null || hoveredBox === null ? (
          <span className="muted">
            Наведите курсор на ящик — покажу сводку; на точку — номер круга и разметку.
            {pinned !== null ? ` Закреплён: ${pinned} (клик снимает).` : ''}
          </span>
        ) : hover.kind === 'point' ? (
          <>
            <span className="key-rect" style={{ backgroundColor: hoveredBox.series.color }} />
            <strong>{hover.driver}</strong>
            <span>круг {hover.point.lap}</span>
            <span className="numeric-strong">{formatDuration(hover.point.value)}</span>
            <span className="muted">
              {!hover.point.counted
                ? `не в расчёте${
                    hover.point.tags.length > 0
                      ? `: ${hover.point.tags
                          .map((tag) => EVENT_LABELS[tag as 'joker' | 'pit'] ?? tag)
                          .join(', ')}`
                      : ''
                  }`
                : `${formatDuration(
                    hover.point.value -
                      (statMode === 'quantile'
                        ? hoveredBox.summary.median
                        : hoveredBox.moments.mean),
                  )} к своему ${statMode === 'quantile' ? 'медианному' : 'среднему'} темпу`}
            </span>
          </>
        ) : statMode === 'quantile' ? (
          <>
            <span className="key-rect" style={{ backgroundColor: hoveredBox.series.color }} />
            <strong>{hover.driver}</strong>
            <span>
              кругов в расчёте: {hoveredBox.counted}
              {hoveredBox.series.points.length > hoveredBox.counted
                ? ` из ${hoveredBox.series.points.length}`
                : ''}
            </span>
            <span>
              медиана <b>{formatDuration(hoveredBox.summary.median)}</b>
            </span>
            <span>
              Q1–Q3 {formatDuration(hoveredBox.summary.q1)}–{formatDuration(hoveredBox.summary.q3)}
            </span>
            <span>IQR {formatDuration(hoveredBox.summary.iqr)}</span>
            <span className="muted">за усами: {hoveredBox.summary.outliers.length}</span>
          </>
        ) : (
          <>
            <span className="key-rect" style={{ backgroundColor: hoveredBox.series.color }} />
            <strong>{hover.driver}</strong>
            <span>
              кругов в расчёте: {hoveredBox.counted}
              {hoveredBox.series.points.length > hoveredBox.counted
                ? ` из ${hoveredBox.series.points.length}`
                : ''}
            </span>
            <span>
              среднее <b>{formatDuration(hoveredBox.moments.mean)}</b>
            </span>
            <span>SD {formatDuration(hoveredBox.moments.sd)}</span>
            <span>
              ±1 SD {formatDuration(hoveredBox.moments.low)}–
              {formatDuration(hoveredBox.moments.high)}
            </span>
          </>
        )}
      </div>
    </>
  )
}
