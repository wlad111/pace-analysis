import { useEffect, useMemo, useState } from 'react'

import type { LapRow } from '../api'
import type { MetricDef } from '../metrics'
import { availableMetrics, metricCoverage } from '../metrics'
import type { DriverSeries } from '../series'
import type { ThemeMode } from '../theme'
import { ViewToggle } from './Card'
import type { DriverStats } from '../api'
import { LapDistribution } from './LapDistribution'
import { PaceProgressChart } from './PaceProgressChart'

const CHART_OPTIONS = [
  { value: 'rolling', label: 'Динамика' },
  { value: 'distribution', label: 'Распределение' },
] as const

type ChartChoice = (typeof CHART_OPTIONS)[number]['value']

export interface PaceExplorerProps {
  laps: readonly LapRow[]
  series: readonly DriverSeries[]
  hidden: ReadonlySet<string>
  onToggle: (driver: string) => void
  onShowAll: () => void
  usedLaps: ReadonlyMap<string, ReadonlySet<number>>
  /** `/stats` rows: the dynamics tab draws its trend lines from the backend slope. */
  statRows: readonly DriverStats[]
  subject: string
  rival: string
  window: number
  onWindow: (window: number) => void
  relative: boolean
  onRelative: (relative: boolean) => void
  mode: ThemeMode
  stale?: boolean
}

/**
 * The centre of the page: one large area, three ways of looking at the same
 * laps, and a metric switch that applies to all of them.
 *
 * Sector metrics are offered only when the data actually carries sectors, and
 * the coverage note is not decoration — Apex sends sector splits only to the
 * recipient of the e-mail, so a sector view is routinely a one-driver chart
 * until more of the same race is imported.
 */
export function PaceExplorer({
  laps,
  series,
  hidden,
  onToggle,
  onShowAll,
  usedLaps,
  statRows,
  subject,
  rival,
  window,
  onWindow,
  relative,
  onRelative,
  mode,
  stale = false,
}: PaceExplorerProps) {
  const [chart, setChart] = useState<ChartChoice>('rolling')
  const [metricId, setMetricId] = useState('lap')

  const metrics = useMemo(() => availableMetrics(laps), [laps])
  const metric: MetricDef = useMemo(
    () => metrics.find((item) => item.id === metricId) ?? metrics[0],
    [metrics, metricId],
  )

  // A session without sectors must not leave the page stuck on a blank S1 view.
  useEffect(() => {
    if (!metrics.some((item) => item.id === metricId)) setMetricId(metrics[0].id)
  }, [metrics, metricId])

  const coverage = useMemo(() => metricCoverage(laps, metric), [laps, metric])
  const metricOptions = useMemo(
    () => metrics.map((item) => ({ value: item.id, label: item.short })),
    [metrics],
  )

  return (
    <section className="explorer" aria-label="Анализ темпа">
      <div className="explorer-bar">
        <ViewToggle
          label="Тип графика"
          value={chart}
          options={CHART_OPTIONS}
          onChange={setChart}
        />
        {metrics.length > 1 && (
          <div className="explorer-metric">
            <span className="explorer-metric-label">Метрика</span>
            <ViewToggle
              label="Метрика: круг целиком или отдельный сектор"
              value={metric.id}
              options={metricOptions}
              onChange={setMetricId}
            />
          </div>
        )}
      </div>

      {metric.sector !== null && !coverage.complete && (
        <p className="explorer-note" role="status">
          Секторные времена есть только у {coverage.covered.length} из {coverage.total} пилотов
          {coverage.covered.length > 0 ? ` (${coverage.covered.join(', ')})` : ''} — тайминг присылает
          их только получателю письма. Импортируйте письма других пилотов этой же гонки, и график
          заполнится: слияние по сессии уже поддерживается.
        </p>
      )}

      {chart === 'rolling' && (
        <PaceProgressChart
          laps={laps}
          rows={statRows}
          usedLaps={usedLaps}
          metric={metric}
          subject={subject}
          rival={rival}
          window={window}
          onWindow={onWindow}
          relative={relative}
          onRelative={onRelative}
          mode={mode}
          stale={stale}
        />
      )}
      {chart === 'distribution' && (
        <LapDistribution
          laps={laps}
          series={series}
          hidden={hidden}
          onToggle={onToggle}
          onShowAll={onShowAll}
          metric={metric}
          usedLaps={usedLaps}
          mode={mode}
          stale={stale}
        />
      )}
    </section>
  )
}
