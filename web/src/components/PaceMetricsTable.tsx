/**
 * The pace metrics table.
 *
 * Every number comes from `/stats`, computed on the used laps only, so the
 * table and the charts describe the same sample. Each header carries the full
 * explanation of its column: these are statistics, and a bare «SD» tells a
 * reader nothing about what it measures or in what units.
 */

import { useMemo, useState } from 'react'

import type { DriverStats } from '../api'
import { formatDuration, formatPValue } from '../format'
import { Card } from './Card'

export interface PaceMetricsTableProps {
  rows: readonly DriverStats[]
  subject: string
  rival: string
  onPick: (driver: string) => void
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
  { key: 'position', label: 'Поз', title: 'Финишная позиция по протоколу гонки', left: true },
  { key: 'driver', label: 'Пилот', title: 'Ник в системе тайминга', left: true },
  {
    key: 'mean_ms',
    label: 'Среднее',
    title:
      'Среднее время зачётного круга, с — основная метрика темпа: суммарное время отрезка это ровно «число кругов × среднее»',
  },
  {
    key: 'median_ms',
    label: 'Медиана',
    title:
      'Медианное время зачётного круга, с — типичный круг. Устойчива к отдельным грязным кругам, но её сумма ничему на секундомере не соответствует',
  },
  {
    key: 'best_ms',
    label: 'Лучший',
    title: 'Быстрейший зачётный круг, с. Не официальный из письма — тот, как правило, джокер',
  },
  {
    key: 'std_ms',
    label: 'Разброс (SD)',
    title:
      'Стандартное отклонение времени круга, с. Грубо: примерно две трети кругов укладываются в «среднее ± SD». Меньше — ровнее едет; лучший в гонке подсвечен',
  },
  {
    key: 'pace_delta_to_best_ms',
    label: 'Отставание',
    title:
      'На сколько секунд среднее этого пилота медленнее лучшего среднего в гонке. Умножьте на число кругов — получите потерю на дистанции',
  },
  {
    key: 'degradation_ms_per_lap',
    label: 'Тренд / 10 кр.',
    title:
      'На сколько секунд менялось время круга за каждые 10 кругов: плюс — замедлялся к концу, минус — разъезжался. Жирным отмечен тренд, который вряд ли объясняется случайностью (p < 0.05), серым — неотличимый от неё',
  },
  {
    key: 'theoretical_best_ms',
    label: 'Идеал',
    title:
      'Сумма лучших секторов, с — недостижимый круг. Считается только там, где письмо принесло секторные времена, то есть у получателя',
  },
  {
    key: 'n_used',
    label: 'Круги',
    title:
      'Справка: сколько кругов взято в обработку из записанных. Остальные исключены как стартовый, размеченный (джокер, пит) или медленный выброс',
  },
]

function sortValue(row: DriverStats, key: ColumnKey): number | string | null {
  if (key === 'driver') return row.driver
  const value = row[key]
  return typeof value === 'number' ? value : null
}

export function PaceMetricsTable({
  rows,
  subject,
  rival,
  onPick,
  stale = false,
}: PaceMetricsTableProps) {
  const [sort, setSort] = useState<{ key: ColumnKey; ascending: boolean }>({
    key: 'mean_ms',
    ascending: true,
  })

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
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const role = row.driver === subject ? 'a' : row.driver === rival ? 'b' : 'other'
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
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="chart-footnote">
        <b>Среднее</b> — основная метрика темпа: суммарное время отрезка это ровно «число кругов ×
        среднее», поэтому отставание в 0.4 с на круге за 90 кругов превращается в 36 секунд реальной
        потери. <b>Медиана</b> — дополнение: она описывает типичный круг и расходится со средним
        тогда, когда распределение скошено. <b>Разброс (SD)</b> — насколько ровно пилот едет:
        примерно две трети кругов укладываются в «среднее ± SD». <b>Отставание</b> — разрыв среднего
        с лучшим средним в гонке. <b>Тренд</b> — на сколько менялось время круга за каждые десять
        кругов; жирным показан тренд с p &lt; 0.05, серым — тот, что не отличим от случайности.
        <b>Идеал</b> — сумма лучших секторов, есть только там, где письмо принесло секторные времена.
        <b>Круги</b> в конце — справка: сколько кругов попало в обработку. Наведите курсор на
        заголовок — там развёрнутое описание колонки.
      </p>
    </Card>
  )
}
