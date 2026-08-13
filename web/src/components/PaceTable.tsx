import { useMemo, useState } from 'react'

import type { DriverStats } from '../api'
import { formatDuration, formatPValue } from '../format'
import type { DriverSeries } from '../series'
import { Card } from './Card'

export interface PaceTableProps {
  rows: readonly DriverStats[]
  series: readonly DriverSeries[]
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

interface Column {
  key: ColumnKey
  label: string
  title: string
  left?: boolean
}

const COLUMNS: readonly Column[] = [
  { key: 'position', label: 'Поз', title: 'Финишная позиция в гонке', left: true },
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
  { key: 'best_ms', label: 'Лучший', title: 'Быстрейший зачётный круг, с (не официальный из письма)' },
  {
    key: 'std_ms',
    label: 'Разброс (SD)',
    title:
      'Стандартное отклонение времени круга, с. Грубо: примерно две трети кругов укладываются в «среднее ± SD». Меньше — ровнее едет',
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
      'На сколько секунд менялось время круга за каждые 10 кругов: плюс — замедлялся к концу, минус — разъезжался. Жирным отмечен тренд, который вряд ли объясняется случайностью (p < 0.05)',
  },
  {
    key: 'theoretical_best_ms',
    label: 'Идеал',
    title: 'Сумма лучших секторов, с — недостижимый круг. Считается только там, где есть секторы',
  },
  {
    key: 'n_used',
    label: 'Круги',
    title: 'Справка: сколько кругов взято в обработку из записанных. Остальные исключены как стартовый, размеченный (джокер, пит) или медленный выброс',
  },
]

/**
 * The OLS slope, restated as «seconds per ten laps».
 *
 * Milliseconds per lap is a true number and an unreadable one: -24 means
 * nothing at a glance. Ten laps is a chunk of a race, and a tenth of a second
 * is a unit a driver feels. A slope the regression cannot separate from noise
 * is shown in grey rather than hidden, because absence of a trend is a result.
 */
function trend(row: DriverStats) {
  const slope = row.degradation_ms_per_lap
  if (slope === null || slope === undefined) return '—'
  const perTen = slope * 10
  const text = `${perTen > 0 ? '+' : perTen < 0 ? '-' : '±'}${formatDuration(Math.abs(perTen))}`
  const p = row.degradation_p_value
  const solid = p !== null && p !== undefined && p < 0.05
  return solid ? <b>{text}</b> : <span className="muted">{text}</span>
}

function sortValue(row: DriverStats, key: ColumnKey): number | string | null {
  if (key === 'driver') return row.driver
  const value = row[key]
  return typeof value === 'number' ? value : null
}

function compare(a: DriverStats, b: DriverStats, key: ColumnKey, ascending: boolean): number {
  const left = sortValue(a, key)
  const right = sortValue(b, key)
  if (left === null && right === null) return 0
  if (left === null) return 1 // unknown values always sink, whatever the direction
  if (right === null) return -1
  const order = typeof left === 'string' ? left.localeCompare(String(right)) : left - Number(right)
  return ascending ? order : -order
}

/**
 * The pace metrics table (SPEC section 6), and the accessible fallback for
 * every chart on the page: it carries the same numbers as text.
 */
export function PaceTable({ rows, series, stale = false }: PaceTableProps) {
  const [sort, setSort] = useState<{ key: ColumnKey; ascending: boolean }>({
    key: 'position',
    ascending: true,
  })

  const colors = useMemo(
    () => new Map(series.map((item) => [item.name, item.color])),
    [series],
  )
  const sorted = useMemo(
    () => [...rows].sort((a, b) => compare(a, b, sort.key, sort.ascending)),
    [rows, sort],
  )

  const onSort = (key: ColumnKey): void => {
    setSort((previous) =>
      previous.key === key
        ? { key, ascending: !previous.ascending }
        : { key, ascending: key === 'position' || key === 'driver' },
    )
  }

  return (
    <Card
      title="Метрики темпа"
      caption="Все метрики считаются только по зачётным кругам, поэтому «Лучший» — это чистый лучший круг, а не официальный из письма. «Круги» — зачётных из записанных; круг могут исключить как стартовый, как размеченный (джокер, пит) или как медленный выброс. Клик по заголовку сортирует."
      stale={stale}
    >
      {sorted.length === 0 ? (
        <p className="empty">В этой сессии нет кругов.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption>
              Все времена в секундах. «*» рядом с ником — у пилота есть подозрительно быстрый
              круг, который фильтр оставил в выборке.
            </caption>
            <thead>
              <tr>
                {COLUMNS.map((column) => (
                  <th
                    key={column.key}
                    scope="col"
                    className={column.left ? 'left sortable' : 'sortable'}
                    title={column.title}
                    aria-sort={
                      sort.key === column.key
                        ? sort.ascending
                          ? 'ascending'
                          : 'descending'
                        : 'none'
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
                const fast = row.suspicious_fast_lap_numbers ?? []
                return (
                  <tr key={row.driver}>
                    <td className="left">{row.position ?? '—'}</td>
                    <th className="left" scope="row">
                      <span
                        className="key-line"
                        style={{
                          display: 'inline-block',
                          width: 14,
                          height: 2,
                          marginRight: 6,
                          verticalAlign: 'middle',
                          backgroundColor: colors.get(row.driver) ?? 'transparent',
                        }}
                        aria-hidden="true"
                      />
                      {row.driver}
                      {fast.length > 0 && (
                        <span title={`Подозрительно быстрые круги: ${fast.join(', ')}`}> *</span>
                      )}
                    </th>
                    <td className="numeric-strong">{formatDuration(row.mean_ms)}</td>
                    <td>{formatDuration(row.median_ms)}</td>
                    <td>{formatDuration(row.best_ms)}</td>
                    <td>{formatDuration(row.std_ms)}</td>
                    <td>
                      {row.pace_delta_to_best_ms === null ||
                      row.pace_delta_to_best_ms === undefined ||
                      row.pace_delta_to_best_ms === 0
                        ? '—'
                        : `+${formatDuration(row.pace_delta_to_best_ms)}`}
                    </td>
                    <td title={`p = ${formatPValue(row.degradation_p_value)}`}>
                      {trend(row)}
                    </td>
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
      )}
      <p className="chart-footnote">
        <b>Среднее</b> — основная метрика темпа: суммарное время отрезка это ровно «число кругов ×
        среднее», поэтому отставание в 0.4 с на круге за 90 кругов превращается в 36 секунд реальной
        потери. <b>Медиана</b> — дополнение: она описывает типичный круг и расходится со средним
        тогда, когда распределение скошено. <b>Разброс (SD)</b> — насколько ровно пилот едет:
        примерно две трети кругов укладываются в «среднее ± SD». <b>Отставание</b> — разрыв среднего
        с лучшим средним в гонке. <b>Тренд</b> — на сколько менялось время круга за каждые десять
        кругов; жирным показан тренд с p &lt; 0.05, серым — тот, что не отличим от случайности.
        <b>Круги</b> в конце — справка: сколько кругов попало в обработку.
      </p>
    </Card>
  )
}
