/**
 * Pace ladder: one row per driver, sorted by the chosen pace metric.
 *
 * Replaces the classification card as the primary list: the number that leads
 * is the pace metric, not the official best lap. Clicking a row picks the
 * driver as B for the head-to-head panel.
 */

import { useMemo } from 'react'

import type { DriverStats } from '../api'
import { formatDuration, formatSignedDuration } from '../format'
import { Card, ViewToggle } from './Card'

export type LadderMetric = 'mean' | 'median'

export interface PaceLadderProps {
  rows: readonly DriverStats[]
  /** Driver A: the page subject. */
  subject: string
  /** Driver B: the comparison partner. */
  rival: string
  metric: LadderMetric
  onMetric: (metric: LadderMetric) => void
  onPick: (driver: string) => void
  stale?: boolean
}

const METRIC_OPTIONS = [
  { value: 'mean' as const, label: 'по среднему' },
  { value: 'median' as const, label: 'по медиане' },
]

function metricValue(row: DriverStats, metric: LadderMetric): number | null {
  return metric === 'median' ? row.median_ms : row.mean_ms
}

/** Percent position of `ms` on the shared spread scale. */
function scalePercent(ms: number, lo: number, hi: number): number {
  return ((ms - lo) / (hi - lo)) * 100
}

export function PaceLadder({
  rows,
  subject,
  rival,
  metric,
  onMetric,
  onPick,
  stale = false,
}: PaceLadderProps) {
  const ranked = useMemo(
    () =>
      [...rows]
        .filter((row) => metricValue(row, metric) !== null)
        .sort((a, b) => (metricValue(a, metric) as number) - (metricValue(b, metric) as number)),
    [rows, metric],
  )

  const scale = useMemo(() => {
    const lows = ranked.map((row) => (row.median_ms ?? 0) - (row.std_ms ?? 0))
    const highs = ranked.map((row) => (row.median_ms ?? 0) + (row.std_ms ?? 0))
    return { lo: Math.min(...lows) - 120, hi: Math.max(...highs) + 120 }
  }, [ranked])

  if (ranked.length === 0) {
    return (
      <Card title="Темп пилотов" stale={stale}>
        <p className="empty">В этой сессии нет зачётных кругов.</p>
      </Card>
    )
  }

  const best = metricValue(ranked[0], metric) as number
  const worstGap = (metricValue(ranked[ranked.length - 1], metric) as number) - best

  return (
    <Card
      title="Темп пилотов"
      caption="Клик по строке — выбрать B для сравнения. Считаются только зачётные круги."
      stale={stale}
      actions={
        <ViewToggle
          label="Метрика списка"
          value={metric}
          options={METRIC_OPTIONS}
          onChange={onMetric}
        />
      }
    >
      <ul className="ladder">
        {ranked.map((row, index) => {
          const own = metricValue(row, metric) as number
          const gap = own - best
          const median = row.median_ms ?? own
          const sd = row.std_ms ?? 0
          const role = row.driver === subject ? 'a' : row.driver === rival ? 'b' : 'other'
          return (
            <li key={row.driver}>
              <button
                type="button"
                className={`ladder-row is-${role}`}
                aria-pressed={role !== 'other'}
                onClick={() => {
                  onPick(row.driver)
                }}
              >
                <span className="ladder-rank">{index + 1}</span>
                <span className="ladder-name">
                  {row.driver}
                  {role === 'a' && <span className="ladder-badge is-a">A</span>}
                  {role === 'b' && <span className="ladder-badge is-b">B</span>}
                </span>
                <span className="ladder-primary">
                  <b>{formatDuration(own)}</b>
                  <small>
                    {metric === 'median' ? 'медиана' : 'среднее'} ·{' '}
                    {metric === 'median'
                      ? `ср. ${formatDuration(row.mean_ms)}`
                      : `мед. ${formatDuration(row.median_ms)}`}
                  </small>
                </span>
                <span className="ladder-plot">
                  <span className="ladder-bar">
                    <span
                      className="ladder-bar-fill"
                      style={{ width: `${worstGap === 0 ? 2 : Math.max(2, (gap / worstGap) * 100)}%` }}
                    />
                  </span>
                  <span className="ladder-band">
                    <span
                      className="ladder-band-range"
                      style={{
                        left: `${scalePercent(median - sd, scale.lo, scale.hi)}%`,
                        width: `${
                          scalePercent(median + sd, scale.lo, scale.hi) -
                          scalePercent(median - sd, scale.lo, scale.hi)
                        }%`,
                      }}
                    />
                    <span
                      className="ladder-band-tick"
                      style={{ left: `${scalePercent(median, scale.lo, scale.hi)}%` }}
                    />
                  </span>
                </span>
                <span className="ladder-gap">
                  <b>{gap === 0 ? 'лучший' : formatSignedDuration(gap)}</b>
                  <small>SD ±{formatDuration(row.std_ms)}</small>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      <p className="chart-footnote">
        Полоса — отставание от лучшего по выбранной метрике. Тонкая линия — интервал «медиана ± SD»
        на общей шкале, засечка — медиана: чем короче линия, тем ровнее пилот.
      </p>
    </Card>
  )
}
