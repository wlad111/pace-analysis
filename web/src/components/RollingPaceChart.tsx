import { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { TooltipContentProps } from 'recharts'

import type { LapRow } from '../api'
import { axisTicks, robustBounds } from '../descriptive'
import { formatClock } from '../format'
import type { MetricDef } from '../metrics'
import { readMetric, rollingMean } from '../metrics'
import type { DriverSeries } from '../series'
import type { ThemeMode } from '../theme'
import { CHART_CHROME } from '../theme'
import { Card, ViewToggle } from './Card'
import { SeriesLegend } from './SeriesLegend'

/** Window ladder, shortest first. Which of these are offered depends on the data. */
const WINDOW_LADDER: readonly number[] = [3, 5, 7, 10, 15, 20, 30]

const DEFAULT_WINDOW = 5

/**
 * Fewest points a smoothed curve must have to be worth drawing. A 15-lap window
 * over a 17-lap sprint yields three points — technically a curve, practically a
 * straight line that invites over-reading. Long windows therefore appear only
 * once the race is long enough to support them.
 */
const MIN_CURVE_POINTS = 5

function lapsWord(count: number): string {
  const tail = count % 100
  if (tail >= 11 && tail <= 14) return 'кругов'
  const last = count % 10
  if (last === 1) return 'кругу'
  if (last >= 2 && last <= 4) return 'круга'
  return 'кругов'
}

export interface RollingPaceChartProps {
  laps: readonly LapRow[]
  series: readonly DriverSeries[]
  hidden: ReadonlySet<string>
  onToggle: (driver: string) => void
  onShowAll: () => void
  metric: MetricDef
  /** Same k the lap filter uses, so the chart and the metrics agree. */
  madK: number
  mode: ThemeMode
  stale?: boolean
}

interface ChartRow {
  lap: number
  [key: string]: number | null
}

const seriesKey = (slot: number): string => `s${slot}`

/**
 * Race dynamics: a trailing moving average per driver.
 *
 * The raw lap-time chart answers «что случилось на круге N»; this one answers
 * «кто разъезжался, а кто сдавал». Smoothing is what makes that readable — a
 * single traffic lap swings the raw line by a second and hides a 50 ms drift
 * that runs across ten laps. Joker and pit laps are dropped before averaging,
 * not averaged in, so one pit stop cannot bend the curve for a whole window.
 */
export function RollingPaceChart({
  laps,
  series,
  hidden,
  onToggle,
  onShowAll,
  metric,
  madK,
  mode,
  stale = false,
}: RollingPaceChartProps) {
  const [choice, setChoice] = useState<string>(String(DEFAULT_WINDOW))
  const chrome = CHART_CHROME[mode]

  // How many pace laps the longest stint has: the ceiling on a useful window.
  const capacity = useMemo(() => {
    const byDriver = readMetric(laps, metric)
    let longest = 0
    for (const item of series) {
      const bucket = byDriver.get(item.name)
      if (bucket === undefined || hidden.has(item.name)) continue
      longest = Math.max(longest, bucket.points.filter((point) => !point.excluded).length)
    }
    return longest
  }, [laps, series, hidden, metric])

  const windowOptions = useMemo(() => {
    const offered = WINDOW_LADDER.filter(
      (size) => size <= Math.max(WINDOW_LADDER[0], capacity - MIN_CURVE_POINTS + 1),
    )
    const usable = offered.length > 0 ? offered : [WINDOW_LADDER[0]]
    return usable.map((size) => ({ value: String(size), label: `${size} ${lapsWord(size)}` }))
  }, [capacity])

  // Switching from an endurance race to a sprint must not leave a 30-lap window
  // selected on a 20-lap stint.
  useEffect(() => {
    if (!windowOptions.some((option) => option.value === choice)) {
      setChoice(windowOptions[windowOptions.length - 1].value)
    }
  }, [windowOptions, choice])

  const window = Number(choice)

  const model = useMemo(() => {
    const byDriver = readMetric(laps, metric)
    const curves = new Map<number, { lap: number; value: number }[]>()
    const visible: DriverSeries[] = []
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    let lastLap = 0
    let excluded = 0
    let outliers = 0
    let shortest = Number.POSITIVE_INFINITY

    for (const item of series) {
      const bucket = byDriver.get(item.name)
      if (bucket === undefined) continue
      // Tagged laps (joker, pit) are known non-pace. On top of that, anything
      // outside the driver's own robust range is dropped too: a second pit stop
      // the detector has not tagged, a spin, a lap behind a safety kart — a
      // single +35 s lap otherwise raises the average by 35/window seconds and
      // parades through the curve as a hump for `window` laps.
      const tagged = bucket.points.filter((point) => point.excluded)
      const bounds = robustBounds(
        bucket.points.filter((point) => !point.excluded).map((point) => point.value),
        madK,
      )
      const points = bucket.points.map((point) => ({
        ...point,
        excluded:
          point.excluded ||
          (bounds !== null && (point.value > bounds.ceiling || point.value < bounds.floor)),
      }))
      const dropped = points.filter((point) => point.excluded).length
      excluded += dropped
      outliers += dropped - tagged.length
      shortest = Math.min(shortest, points.length - dropped)
      if (hidden.has(item.name)) continue
      const curve = rollingMean(points, window)
      if (curve.length === 0) continue
      visible.push(item)
      curves.set(item.slot, curve)
      for (const point of curve) {
        lo = Math.min(lo, point.value)
        hi = Math.max(hi, point.value)
        lastLap = Math.max(lastLap, point.lap)
      }
    }

    const rows: ChartRow[] = []
    if (visible.length > 0) {
      const firstLap = Math.min(
        ...[...curves.values()].map((curve) => curve[0]?.lap ?? Number.POSITIVE_INFINITY),
      )
      for (let lap = firstLap; lap <= lastLap; lap += 1) {
        const row: ChartRow = { lap }
        for (const [slot, curve] of curves) {
          row[seriesKey(slot)] = curve.find((point) => point.lap === lap)?.value ?? null
        }
        rows.push(row)
      }
    }
    // A moving average of a flat stint is a flat line; padding keeps it off the
    // frame edge instead of letting the axis collapse onto the data.
    const span = hi - lo
    const pad = Math.max(120, span * 0.25)
    return {
      rows,
      visible,
      excluded,
      outliers,
      shortest: Number.isFinite(shortest) ? shortest : 0,
      lo: Number.isFinite(lo) ? Math.floor(lo - pad) : 0,
      hi: Number.isFinite(hi) ? Math.ceil(hi + pad) : 0,
    }
  }, [laps, series, hidden, metric, window, madK])

  const ticks = model.rows.length === 0 ? [] : axisTicks(model.lo, model.hi, 5)
  const tooLong = model.shortest > 0 && window > model.shortest

  // Recharts types `content` as the generic `ContentType`, so the renderer has
  // to accept generic props and narrow inside.
  const tooltip = (props: TooltipContentProps) => {
    if (props.active !== true || !Array.isArray(props.payload) || props.payload.length === 0) {
      return null
    }
    const rows = props.payload
      .map((item) => {
        const slot = Number(String(item.dataKey).slice(1))
        const driver = model.visible.find((entry) => entry.slot === slot)
        return driver === undefined || typeof item.value !== 'number'
          ? null
          : { driver, value: item.value }
      })
      .filter((row): row is { driver: DriverSeries; value: number } => row !== null)
      .sort((a, b) => a.value - b.value)
    if (rows.length === 0) return null
    return (
      <div className="tooltip">
        <div className="tooltip-title">
          Круг {String(props.label)} · среднее за {window}
        </div>
        {rows.map((row) => (
          <div className="tooltip-row" key={row.driver.name}>
            <span className="key-line" style={{ backgroundColor: row.driver.color }} />
            <span className="name">{row.driver.name}</span>
            <span className="value">{formatClock(row.value)}</span>
          </div>
        ))}
      </div>
    )
  }

  return (
    <Card
      title="Динамика по ходу гонки"
      caption={`Скользящее среднее по ${window} кругам: точка над кругом N — средний темп на последних ${window} зачётных кругах. Аномальные круги выброшены до усреднения, поэтому пит или разворот не оставляют на кривой горба.`}
      stale={stale}
      actions={
        <ViewToggle
          label="Окно сглаживания"
          value={choice}
          options={windowOptions}
          onChange={setChoice}
        />
      }
    >
      {model.rows.length === 0 ? (
        <p className="empty">
          {tooLong
            ? `Окно ${window} кругов длиннее, чем зачётных кругов у пилотов (${model.shortest}). Возьмите окно короче.`
            : 'Нет данных для сглаживания — проверьте выбор пилотов в легенде.'}
        </p>
      ) : (
        <>
          <SeriesLegend
            series={series}
            hidden={hidden}
            onToggle={onToggle}
            onShowAll={onShowAll}
            label="Пилоты на графике динамики"
          />
          <div className="chart-frame">
            <ResponsiveContainer width="100%" height={340}>
              <LineChart data={model.rows} margin={{ top: 10, right: 16, bottom: 8, left: 8 }}>
                <CartesianGrid stroke={chrome.grid} vertical={false} />
                <XAxis
                  dataKey="lap"
                  stroke={chrome.axis}
                  tick={{ fill: chrome.muted, fontSize: 11 }}
                  tickLine={false}
                  label={{
                    value: 'Круг',
                    position: 'insideBottom',
                    offset: -4,
                    fill: chrome.muted,
                    fontSize: 11,
                  }}
                />
                <YAxis
                  domain={[model.lo, model.hi]}
                  ticks={ticks}
                  stroke={chrome.axis}
                  tick={{ fill: chrome.muted, fontSize: 11 }}
                  tickLine={false}
                  width={64}
                  tickFormatter={(value: number) => formatClock(value)}
                />
                <Tooltip content={tooltip} cursor={{ stroke: chrome.axis, strokeWidth: 1 }} />
                {model.visible.map((item) => (
                  <Line
                    key={item.name}
                    type="monotone"
                    dataKey={seriesKey(item.slot)}
                    name={item.name}
                    stroke={item.color}
                    strokeWidth={2.5}
                    dot={false}
                    activeDot={{ r: 4 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      <p className="chart-footnote">
        Кривая начинается с {window}-го зачётного круга — раньше усреднять нечего. Из усреднения
        исключены размеченные круги (джокер, пит) и всё, что вышло за робастный диапазон пилота
        (медиана ± {madK} · MAD): один круг на +35 с иначе поднял бы среднее на {Math.round(35 / window)} с
        и тянулся бы горбом целое окно.
        {model.excluded > 0
          ? ` Выброшено кругов: ${model.excluded}${model.outliers > 0 ? `, из них ${model.outliers} по статистике, а не по разметке` : ''}.`
          : ''}{' '}
        Ось Y подрезана по данным, поэтому наклон здесь читается сильнее, чем на графике времён кругов —
        сравнивайте форму кривых, а не абсолютную высоту подъёма.
      </p>
    </Card>
  )
}
