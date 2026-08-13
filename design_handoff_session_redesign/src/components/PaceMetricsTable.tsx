/**
 * The pace metrics table: the columns of the current `PaceTable`, plus a
 * distribution cell per driver (laps, IQR box, median, mean) drawn on a scale
 * shared by every row, so «кто ровнее» is answered without reading numbers.
 *
 * Numbers come from `/stats`; the individual laps behind the box come from the
 * session payload filtered by `used_lap_numbers`, so the box never contains a
 * lap the metrics excluded.
 */

import { useMemo, useState } from 'react'

import type { DriverStats, LapRow } from '../api'
import { formatDuration, formatPValue } from '../format'
import type { ThemeMode } from '../theme'
import { CHART_CHROME, seriesColor } from '../theme'
import { Card } from './Card'

export interface PaceMetricsTableProps {
  rows: readonly DriverStats[]
  laps: readonly LapRow[]
  usedLaps: ReadonlyMap<string, ReadonlySet<number>>
  subject: string
  rival: string
  onPick: (driver: string) => void
  mode: ThemeMode
  stale?: boolean
}

type ColumnKey =
  | 'position'
  | 'driver'
  | 'mean_ms'
  | 'median_ms'
  | 'best_ms'
  | 'std_ms'
  | 'pace_delta_to_best_ms'
  | 'degradation_ms_per_lap'
  | 'theoretical_best_ms'
  | 'n_used'

const COLUMNS: readonly { key: ColumnKey; label: string; title: string; left?: boolean }[] = [
  { key: 'position', label: 'Поз', title: 'Позиция на финише по протоколу', left: true },
  { key: 'driver', label: 'Пилот', title: 'Ник в системе тайминга', left: true },
  { key: 'mean_ms', label: 'Среднее', title: 'Суммарное время отрезка = число кругов × среднее' },
  { key: 'median_ms', label: 'Медиана', title: 'Типичный круг, устойчива к отдельным грязным кругам' },
  { key: 'best_ms', label: 'Лучший', title: 'Быстрейший зачётный круг, не официальный из письма' },
  { key: 'std_ms', label: 'Разброс SD', title: 'Две трети кругов укладываются в «среднее ± SD»' },
  { key: 'pace_delta_to_best_ms', label: 'Отставание', title: 'Разрыв среднего с лучшим средним в гонке' },
  { key: 'degradation_ms_per_lap', label: 'Тренд / 10 кр.', title: 'Наклон OLS: плюс — замедлялся к концу' },
  { key: 'theoretical_best_ms', label: 'Идеал', title: 'Сумма лучших секторов — есть только у получателя письма' },
  { key: 'n_used', label: 'Круги', title: 'Зачётных из записанных' },
]

const BOX_WIDTH = 440
const BOX_HEIGHT = 26

function quantile(sorted: readonly number[], q: number): number {
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

/** Deterministic vertical jitter so the strip does not stack points. */
function jitter(seed: string, index: number): number {
  let value = index + 1
  for (let i = 0; i < seed.length; i += 1) value = (value * 31 + seed.charCodeAt(i)) % 997
  return (value / 997 - 0.5) * 13
}

function sortValue(row: DriverStats, key: ColumnKey): number | string | null {
  if (key === 'driver') return row.driver
  const value = row[key]
  return typeof value === 'number' ? value : null
}

export function PaceMetricsTable({
  rows,
  laps,
  usedLaps,
  subject,
  rival,
  onPick,
  mode,
  stale = false,
}: PaceMetricsTableProps) {
  const [sort, setSort] = useState<{ key: ColumnKey; ascending: boolean }>({
    key: 'mean_ms',
    ascending: true,
  })
  const chrome = CHART_CHROME[mode]

  const samples = useMemo(() => {
    const byDriver = new Map<string, number[]>()
    for (const lap of laps) {
      if (lap.time_ms === null) continue
      const used = usedLaps.get(lap.driver)
      if (used !== undefined && !used.has(lap.lap_number)) continue
      const bucket = byDriver.get(lap.driver) ?? []
      bucket.push(lap.time_ms)
      byDriver.set(lap.driver, bucket)
    }
    for (const [driver, values] of byDriver) byDriver.set(driver, values.sort((a, b) => a - b))
    return byDriver
  }, [laps, usedLaps])

  const scale = useMemo(() => {
    const all = [...samples.values()].flat()
    if (all.length === 0) return { lo: 0, hi: 1 }
    return { lo: Math.min(...all) - 80, hi: Math.max(...all) + 80 }
  }, [samples])

  const sorted = useMemo(() => {
    return [...rows].sort((a, b) => {
      const left = sortValue(a, sort.key)
      const right = sortValue(b, sort.key)
      if (left === null && right === null) return 0
      if (left === null) return 1
      if (right === null) return -1
      const order =
        typeof left === 'string' ? left.localeCompare(String(right)) : left - Number(right)
      return sort.ascending ? order : -order
    })
  }, [rows, sort])

  const bestSd = Math.min(...rows.map((row) => row.std_ms ?? Number.POSITIVE_INFINITY))
  const bx = (ms: number): number => ((ms - scale.lo) / (scale.hi - scale.lo)) * BOX_WIDTH

  const onSort = (key: ColumnKey): void => {
    setSort((previous) =>
      previous.key === key
        ? { key, ascending: !previous.ascending }
        : { key, ascending: key !== 'n_used' },
    )
  }

  const trend = (row: DriverStats) => {
    const slope = row.degradation_ms_per_lap
    if (slope === null || slope === undefined) return <span className="muted">—</span>
    const perTen = slope * 10
    // Below display precision a signed zero is noise dressed as a result.
    if (Math.abs(perTen) < 5) return <span className="muted">±0.00</span>
    const text = `${perTen > 0 ? '+' : '-'}${formatDuration(Math.abs(perTen))}`
    const p = row.degradation_p_value
    return p !== null && p !== undefined && p < 0.05 ? <b>{text}</b> : <span className="muted">{text}</span>
  }

  return (
    <Card
      title="Метрики темпа"
      caption="Только зачётные круги. Клик по заголовку сортирует, клик по строке выбирает B."
      stale={stale}
    >
      <div className="table-wrap">
        <table className="data metrics">
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={column.left ? 'left sortable' : 'sortable'}
                  title={column.title}
                  aria-sort={
                    sort.key === column.key ? (sort.ascending ? 'ascending' : 'descending') : 'none'
                  }
                >
                  <button
                    type="button"
                    onClick={() => {
                      onSort(column.key)
                    }}
                  >
                    {column.label}
                    <span className="arrow" aria-hidden="true">
                      {sort.key === column.key ? (sort.ascending ? '▲' : '▼') : ''}
                    </span>
                  </button>
                </th>
              ))}
              <th className="left" scope="col">
                Распределение
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const role = row.driver === subject ? 'a' : row.driver === rival ? 'b' : 'other'
              const values = samples.get(row.driver) ?? []
              const stroke =
                role === 'a' ? chrome.accent : role === 'b' ? seriesColor(1, mode) : chrome.muted
              return (
                <tr
                  key={row.driver}
                  className={`is-${role}`}
                  onClick={() => {
                    onPick(row.driver)
                  }}
                >
                  <td className="left">{row.position ?? '—'}</td>
                  <th className="left" scope="row">
                    {row.driver}
                    {role === 'a' && <span className="ladder-badge is-a">A</span>}
                    {role === 'b' && <span className="ladder-badge is-b">B</span>}
                  </th>
                  <td className="numeric-strong">{formatDuration(row.mean_ms)}</td>
                  <td>{formatDuration(row.median_ms)}</td>
                  <td>{formatDuration(row.best_ms)}</td>
                  <td className={row.std_ms === bestSd ? 'is-good' : undefined}>
                    {formatDuration(row.std_ms)}
                  </td>
                  <td>
                    {row.pace_delta_to_best_ms
                      ? `+${formatDuration(row.pace_delta_to_best_ms)}`
                      : '—'}
                  </td>
                  <td title={`p = ${formatPValue(row.degradation_p_value)}`}>{trend(row)}</td>
                  <td>{formatDuration(row.theoretical_best_ms)}</td>
                  <td className="muted">
                    {row.n_used} / {row.n_laps}
                  </td>
                  <td className="left">
                    {values.length > 1 && (
                      <svg
                        viewBox={`0 0 ${BOX_WIDTH} ${BOX_HEIGHT}`}
                        width="100%"
                        height={BOX_HEIGHT}
                        aria-hidden="true"
                      >
                        <line
                          x1={bx(values[0])}
                          y1="13"
                          x2={bx(values[values.length - 1])}
                          y2="13"
                          stroke={stroke}
                        />
                        <rect
                          x={bx(quantile(values, 0.25))}
                          y="5"
                          width={bx(quantile(values, 0.75)) - bx(quantile(values, 0.25))}
                          height="16"
                          rx="2"
                          fill={stroke}
                          fillOpacity={role === 'other' ? 0.12 : 0.28}
                          stroke={stroke}
                        />
                        <line
                          x1={bx(quantile(values, 0.5))}
                          y1="3"
                          x2={bx(quantile(values, 0.5))}
                          y2="23"
                          stroke={chrome.textPrimary}
                          strokeWidth="2"
                        />
                        {values.map((value, index) => (
                          <circle
                            key={`${value}-${index}`}
                            cx={bx(value)}
                            cy={13 + jitter(row.driver, index)}
                            r="1.8"
                            fill={stroke}
                            fillOpacity="0.75"
                          />
                        ))}
                      </svg>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="chart-footnote">
        <b>Среднее</b> — основная метрика темпа: суммарное время отрезка это «число кругов ×
        среднее». <b>Тренд</b> — наклон OLS за десять кругов, жирным p &lt; 0.05, серым — не
        отличимый от случайности. <b>Идеал</b> считается только там, где письмо принесло секторы.
      </p>
    </Card>
  )
}
