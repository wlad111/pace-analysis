import { useMemo, useState } from 'react'

import type { LapRow } from '../api'
import { fiveNumberSummary, mean, std } from '../descriptive'
import { effectiveTagsOf } from '../events'
import { formatDuration } from '../format'
import type { MetricDef } from '../metrics'
import type { DriverSeries } from '../series'
import type { ThemeMode } from '../theme'
import type { BoxSeries, BoxStatMode } from './BoxPlot'
import { BoxPlot } from './BoxPlot'
import { Card, ViewToggle } from './Card'
import { SeriesLegend } from './SeriesLegend'

const VIEW_OPTIONS = [
  { value: 'chart', label: 'График' },
  { value: 'table', label: 'Таблица' },
] as const

const STAT_OPTIONS = [
  { value: 'quantile', label: 'Медиана и квартили' },
  { value: 'moments', label: 'Среднее и SD' },
] as const

const ORDER_OPTIONS = [
  { value: 'classification', label: 'По позиции' },
  { value: 'median', label: 'По центру' },
  { value: 'spread', label: 'По разбросу' },
] as const

type ViewMode = (typeof VIEW_OPTIONS)[number]['value']
type OrderMode = (typeof ORDER_OPTIONS)[number]['value']

export interface LapDistributionProps {
  laps: readonly LapRow[]
  series: readonly DriverSeries[]
  hidden: ReadonlySet<string>
  onToggle: (driver: string) => void
  onShowAll: () => void
  metric: MetricDef
  /** Laps counted by `/stats`, per driver; empty until the stats arrive. */
  usedLaps: ReadonlyMap<string, ReadonlySet<number>>
  mode: ThemeMode
  stale?: boolean
}

/**
 * Distribution of one metric per driver.
 *
 * Two summaries of the same laps, switchable: the Tukey box (median, IQR) and
 * mean ± SD. They part company exactly when the sample is skewed — which is
 * the interesting case, because every t-test on the page reasons about the
 * second while the eye tends to read the first.
 */
export function LapDistribution({
  laps,
  series,
  hidden,
  onToggle,
  onShowAll,
  metric,
  usedLaps,
  mode,
  stale = false,
}: LapDistributionProps) {
  const [view, setView] = useState<ViewMode>('chart')
  const [statMode, setStatMode] = useState<BoxStatMode>('quantile')
  const [order, setOrder] = useState<OrderMode>('classification')

  const boxes = useMemo<BoxSeries[]>(() => {
    const byDriver = new Map<string, BoxSeries>()
    for (const lap of laps) {
      const value = metric.value(lap)
      if (value === null) continue
      const bucket = byDriver.get(lap.driver)
      const tags = effectiveTagsOf(lap)
      const used = usedLaps.get(lap.driver)
      const point = {
        lap: lap.lap_number,
        value,
        tags,
        counted:
          used === undefined
            ? !tags.some((tag) => tag === 'joker' || tag === 'pit')
            : used.has(lap.lap_number),
      }
      if (bucket === undefined) {
        byDriver.set(lap.driver, { name: lap.driver, color: '', points: [point] })
      } else {
        bucket.points.push(point)
      }
    }
    const result: BoxSeries[] = []
    for (const item of series) {
      if (hidden.has(item.name)) continue
      const bucket = byDriver.get(item.name)
      if (bucket === undefined || bucket.points.length === 0) continue
      result.push({ ...bucket, color: item.color })
    }
    const centre = (item: BoxSeries): number => {
      const values = item.points.filter((point) => point.counted).map((point) => point.value)
      return statMode === 'quantile' ? (fiveNumberSummary(values)?.median ?? 0) : mean(values)
    }
    const spread = (item: BoxSeries): number => {
      const values = item.points.filter((point) => point.counted).map((point) => point.value)
      return statMode === 'quantile' ? (fiveNumberSummary(values)?.iqr ?? 0) : (std(values) ?? 0)
    }
    if (order === 'median') result.sort((a, b) => centre(a) - centre(b))
    if (order === 'spread') result.sort((a, b) => spread(a) - spread(b))
    return result
  }, [laps, series, hidden, metric, order, statMode, usedLaps])

  const rows = useMemo(
    () =>
      boxes.map((item) => {
        const values = item.points.filter((point) => point.counted).map((point) => point.value)
        return {
          name: item.name,
          summary: fiveNumberSummary(values),
          mean: mean(values),
          sd: std(values),
          count: values.length,
        }
      }),
    [boxes],
  )

  const unit = metric.sector === null ? 'кругов' : `отрезков ${metric.short}`
  const caption =
    statMode === 'quantile'
      ? `По одному ящику на пилота: ящик — межквартильный размах, толстая линия — медиана, усы дотягиваются до самого дальнего значения в пределах 1.5 × IQR, а рядом точками нанесены все ${unit}: цветные — в расчёте, серые — исключённые.`
      : `По одному ящику на пилота: ящик — среднее ± одно стандартное отклонение, толстая линия — среднее, усы — ± два SD (примерно 95% значений при нормальном распределении), рядом точками нанесены все ${unit}: цветные — в расчёте, серые — исключённые.`

  return (
    <Card
      title={`Распределение темпа · ${metric.label}`}
      caption={`${caption} Наведите курсор на ящик или точку, кликните — чтобы закрепить пилота.`}
      stale={stale}
      actions={
        <ViewToggle label="Вид распределения" value={view} options={VIEW_OPTIONS} onChange={setView} />
      }
    >
      <div className="explorer-bar">
        <SeriesLegend
          series={series}
          hidden={hidden}
          onToggle={onToggle}
          onShowAll={onShowAll}
          keyShape="rect"
          label="Пилоты на графике распределения"
        />
        <div className="explorer-metric">
          <ViewToggle
            label="Что показывает ящик"
            value={statMode}
            options={STAT_OPTIONS}
            onChange={setStatMode}
          />
          {view === 'chart' && (
            <ViewToggle
              label="Порядок ящиков"
              value={order}
              options={ORDER_OPTIONS}
              onChange={setOrder}
            />
          )}
        </div>
      </div>

      {view === 'chart' ? (
        <BoxPlot
          series={boxes}
          statMode={statMode}
          mode={mode}
          emptyText="Нет данных для этой метрики — проверьте выбор пилотов в легенде."
        />
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption>
              {statMode === 'quantile'
                ? 'Пятичисловая сводка по зачётным кругам — без джокера, пита и выбросов.'
                : 'Среднее и стандартное отклонение по зачётным кругам — без джокера, пита и выбросов.'}
            </caption>
            <thead>
              {statMode === 'quantile' ? (
                <tr>
                  <th className="left" scope="col">
                    Пилот
                  </th>
                  <th scope="col">Мин</th>
                  <th scope="col">Q1</th>
                  <th scope="col">Медиана</th>
                  <th scope="col">Q3</th>
                  <th scope="col">Макс</th>
                  <th scope="col">IQR</th>
                  <th scope="col">За 1.5 × IQR</th>
                </tr>
              ) : (
                <tr>
                  <th className="left" scope="col">
                    Пилот
                  </th>
                  <th scope="col">Значений</th>
                  <th scope="col">Среднее</th>
                  <th scope="col">SD</th>
                  <th scope="col">−1 SD</th>
                  <th scope="col">+1 SD</th>
                </tr>
              )}
            </thead>
            <tbody>
              {rows.map((row) =>
                statMode === 'quantile' ? (
                  row.summary === null ? null : (
                    <tr key={row.name}>
                      <th className="left" scope="row">
                        {row.name}
                      </th>
                      <td>{formatDuration(row.summary.min)}</td>
                      <td>{formatDuration(row.summary.q1)}</td>
                      <td className="numeric-strong">{formatDuration(row.summary.median)}</td>
                      <td>{formatDuration(row.summary.q3)}</td>
                      <td>{formatDuration(row.summary.max)}</td>
                      <td>{formatDuration(row.summary.iqr)}</td>
                      <td>{row.summary.outliers.length}</td>
                    </tr>
                  )
                ) : (
                  <tr key={row.name}>
                    <th className="left" scope="row">
                      {row.name}
                    </th>
                    <td>{row.count}</td>
                    <td className="numeric-strong">{formatDuration(row.mean)}</td>
                    <td>{formatDuration(row.sd)}</td>
                    <td>{formatDuration(row.sd === null ? null : row.mean - row.sd)}</td>
                    <td>{formatDuration(row.sd === null ? null : row.mean + row.sd)}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
      <p className="chart-footnote">
        Ящик строится ровно по тем кругам, что учтены в «Метриках темпа», — джокер, пит и медленные
        выбросы в него не входят. Сами круги никуда не деваются: они нанесены серыми точками, чтобы
        было видно, что и когда было исключено.
        {statMode === 'moments'
          ? ' Среднее и SD чувствительны к выбросам: один медленный круг сдвигает обе величины, тогда как медиана и квартили его почти не замечают — если два режима расходятся, распределение скошено.'
          : ''}
      </p>
    </Card>
  )
}
