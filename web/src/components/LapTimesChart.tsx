import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { DotItemDotProps, TooltipContentProps } from 'recharts'

import type { LapRow } from '../api'
import { axisTicks, robustBounds } from '../descriptive'
import type { EventKind, LapEvent } from '../events'
import { EVENT_LABELS } from '../events'
import { formatClock } from '../format'
import type { MetricDef } from '../metrics'
import type { DriverSeries } from '../series'
import type { ThemeMode } from '../theme'
import { CHART_CHROME } from '../theme'
import { Card, ViewToggle } from './Card'
import { SeriesLegend } from './SeriesLegend'

/** How laps that fall outside the robust range are drawn. */
type ScaleMode = 'clamp' | 'hide' | 'full'

const SCALE_OPTIONS: readonly { value: ScaleMode; label: string }[] = [
  { value: 'clamp', label: 'Прижать выбросы' },
  { value: 'hide', label: 'Скрыть выбросы' },
  { value: 'full', label: 'Полная шкала' },
]

const VIEW_OPTIONS = [
  { value: 'chart', label: 'График' },
  { value: 'table', label: 'Таблица' },
] as const

type ViewMode = (typeof VIEW_OPTIONS)[number]['value']

const EVENT_MARK_KINDS: readonly EventKind[] = ['joker', 'pit']

export interface LapTimesChartProps {
  laps: readonly LapRow[]
  series: readonly DriverSeries[]
  hidden: ReadonlySet<string>
  onToggle: (driver: string) => void
  onShowAll: () => void
  /** Same k the lap filter uses, so the chart and the metrics agree. */
  madK: number
  /** Joker / pit laps keyed by lap id (SPEC §10): drawn, but never plotted as pace. */
  eventsByLapId: ReadonlyMap<number, LapEvent>
  /** Whole lap or one sector — the explorer switches this for every chart at once. */
  metric: MetricDef
  mode: ThemeMode
  stale?: boolean
}

/** One lap of one driver, as read out of the payload. */
interface Cell {
  time: number | null
  event: LapEvent | null
}

interface Point {
  time: number | null
  /** 1 above the axis ceiling, -1 below the axis floor, 0 inside. */
  clamped: -1 | 0 | 1
  suspiciousFast: boolean
  event: LapEvent | null
}

interface ChartRow {
  lap: number
  [key: string]: number | null
}

const seriesKey = (slot: number): string => `s${slot}`
const highKey = (slot: number): string => `h${slot}`
const lowKey = (slot: number): string => `l${slot}`
const eventKey = (slot: number, kind: EventKind): string => `${kind === 'joker' ? 'j' : 'p'}${slot}`

/**
 * Triangle pinned near an axis edge: "this lap is off the scale, that way".
 * The 2px surface ring keeps it legible where it overlaps a line.
 */
function outlierMarker(
  color: string,
  surface: string,
  direction: 1 | -1,
): (dot: DotItemDotProps) => ReactNode {
  return (dot) => {
    const { cx, cy } = dot
    if (cx === undefined || cy === undefined || dot.value === null || dot.value === undefined) {
      return null
    }
    const size = 5
    const tip = direction === 1 ? cy - size : cy + size
    const base = direction === 1 ? cy + size : cy - size
    const path = `M ${cx - size} ${base} L ${cx + size} ${base} L ${cx} ${tip} Z`
    return (
      <path
        d={path}
        fill={color}
        stroke={surface}
        strokeWidth={2}
        strokeLinejoin="round"
        pointerEvents="none"
      />
    )
  }
}

/**
 * Joker and pit laps get their own shapes — a diamond and a square — in the
 * driver's colour, so the event type never rests on colour alone.
 */
function eventMarker(
  color: string,
  surface: string,
  kind: EventKind,
): (dot: DotItemDotProps) => ReactNode {
  return (dot) => {
    const { cx, cy } = dot
    if (cx === undefined || cy === undefined || dot.value === null || dot.value === undefined) {
      return null
    }
    const size = 5
    if (kind === 'joker') {
      return (
        <path
          d={`M ${cx} ${cy - size - 1} L ${cx + size + 1} ${cy} L ${cx} ${cy + size + 1} L ${cx - size - 1} ${cy} Z`}
          fill={color}
          stroke={surface}
          strokeWidth={2}
          strokeLinejoin="round"
          pointerEvents="none"
        />
      )
    }
    return (
      <rect
        x={cx - size}
        y={cy - size}
        width={size * 2}
        height={size * 2}
        rx={1}
        fill={color}
        stroke={surface}
        strokeWidth={2}
        pointerEvents="none"
      />
    )
  }
}

export function LapTimesChart({
  laps,
  series,
  hidden,
  onToggle,
  onShowAll,
  madK,
  eventsByLapId,
  metric,
  mode,
  stale = false,
}: LapTimesChartProps) {
  const [scale, setScale] = useState<ScaleMode>('clamp')
  const [view, setView] = useState<ViewMode>('chart')
  const chrome = CHART_CHROME[mode]
  const visible = useMemo(
    () => series.filter((item) => !hidden.has(item.name)),
    [series, hidden],
  )

  const model = useMemo(() => {
    const lapNumbers = [...new Set(laps.map((lap) => lap.lap_number))].sort((a, b) => a - b)
    const byDriver = new Map<string, Map<number, Cell>>()
    for (const lap of laps) {
      let cells = byDriver.get(lap.driver)
      if (cells === undefined) {
        cells = new Map<number, Cell>()
        byDriver.set(lap.driver, cells)
      }
      cells.set(lap.lap_number, {
        time: metric.value(lap),
        event: eventsByLapId.get(lap.id) ?? null,
      })
    }

    // The joker (~1.9 s quick) and the pit lap (~13 s slow) are not pace, so
    // they never get a vote on the scale: they are drawn as markers instead.
    const pooled: number[] = []
    const everything: number[] = []
    for (const item of visible) {
      const cells = byDriver.get(item.name)
      if (cells === undefined) continue
      for (const cell of cells.values()) {
        if (cell.time === null) continue
        everything.push(cell.time)
        if (cell.event === null) pooled.push(cell.time)
      }
    }

    if (pooled.length === 0) {
      return { lapNumbers, rows: [] as ChartRow[], domain: [0, 1] as [number, number], ticks: [] as number[], points: new Map<string, Map<number, Point>>(), outliers: 0, events: [] as LapEvent[] }
    }

    const bounds = robustBounds(pooled, madK)
    // Only the slow side distorts a lap chart (a 40 s incident lap against a
    // 28 s field). A "suspicious fast" lap sits a second or two under the
    // median, so it stays on the scale unless it is wildly out.
    const hardFloor = bounds === null ? -Infinity : bounds.centre - 3 * madK * bounds.spread
    const ceiling = bounds === null ? Infinity : bounds.ceiling
    const inside = pooled.filter((value) => value <= ceiling && value >= hardFloor)
    const source = inside.length > 0 ? inside : pooled
    const lo = Math.min(...source)
    const hi = Math.max(...source)
    const pad = Math.max(150, (hi - lo) * 0.08)
    const robustDomain: [number, number] = [Math.floor(lo - pad), Math.ceil(hi + pad)]
    const fullDomain: [number, number] = [
      Math.floor(Math.min(...everything) - pad),
      Math.ceil(Math.max(...everything) + pad),
    ]
    const domain = scale === 'full' ? fullDomain : robustDomain

    const points = new Map<string, Map<number, Point>>()
    let outliers = 0
    for (const item of series) {
      const cells = byDriver.get(item.name)
      const perLap = new Map<number, Point>()
      for (const lapNumber of lapNumbers) {
        const cell = cells?.get(lapNumber)
        const time = cell?.time ?? null
        if (time === null) {
          perLap.set(lapNumber, { time: null, clamped: 0, suspiciousFast: false, event: null })
          continue
        }
        const above = time > domain[1]
        const below = time < domain[0]
        const event = cell?.event ?? null
        if ((above || below) && event === null && !hidden.has(item.name)) outliers += 1
        perLap.set(lapNumber, {
          time,
          clamped: above ? 1 : below ? -1 : 0,
          suspiciousFast: bounds !== null && event === null && time < bounds.floor,
          event,
        })
      }
      points.set(item.name, perLap)
    }

    // Markers sit a hair inside the axis so the whole glyph stays visible.
    const inset = (domain[1] - domain[0]) * 0.025
    const pin = (value: number): number =>
      Math.min(Math.max(value, domain[0] + inset), domain[1] - inset)
    const rows: ChartRow[] = lapNumbers.map((lapNumber) => {
      const row: ChartRow = { lap: lapNumber }
      for (const item of visible) {
        const point = points.get(item.name)?.get(lapNumber)
        row[eventKey(item.slot, 'joker')] = null
        row[eventKey(item.slot, 'pit')] = null
        if (point === undefined || point.time === null) {
          row[seriesKey(item.slot)] = null
          row[highKey(item.slot)] = null
          row[lowKey(item.slot)] = null
          continue
        }
        if (point.event !== null) {
          // The pace line breaks across a joker or a pit lap: connecting them
          // would draw a spike that is not a change of pace.
          row[seriesKey(item.slot)] = scale === 'full' ? point.time : null
          row[highKey(item.slot)] = null
          row[lowKey(item.slot)] = null
          row[eventKey(item.slot, point.event.kind)] = pin(point.time)
          continue
        }
        const off = point.clamped !== 0
        row[seriesKey(item.slot)] = off && scale === 'hide' ? null : point.time
        row[highKey(item.slot)] =
          scale === 'clamp' && point.clamped === 1 ? domain[1] - inset : null
        row[lowKey(item.slot)] = scale === 'clamp' && point.clamped === -1 ? domain[0] + inset : null
      }
      return row
    })

    const events = visible
      .flatMap((item) =>
        [...(points.get(item.name)?.values() ?? [])]
          .map((point) => point.event)
          .filter((event): event is LapEvent => event !== null),
      )
      .sort((a, b) => a.kind.localeCompare(b.kind) || a.lapNumber - b.lapNumber)

    return {
      lapNumbers,
      rows,
      domain,
      ticks: axisTicks(domain[0], domain[1], 6),
      points,
      outliers,
      events,
    }
  }, [laps, series, visible, hidden, madK, scale, eventsByLapId, metric])

  // Recharts types `<Tooltip content>` as `ContentType<ValueType, NameType>`,
  // so the renderer must accept the generic props and narrow inside — here the
  // only thing read from them is the category label, i.e. the lap number.
  const renderTooltip = (props: TooltipContentProps) => {
    if (!props.active || props.label === undefined) return null
    const lapNumber = Number(props.label)
    if (!Number.isFinite(lapNumber)) return null
    const rows = visible
      .map((item) => ({ item, point: model.points.get(item.name)?.get(lapNumber) }))
      .filter((entry): entry is { item: DriverSeries; point: Point } => entry.point !== undefined)
      .sort((a, b) => (a.point.time ?? Infinity) - (b.point.time ?? Infinity))
    if (rows.length === 0) return null
    const hasFlag = rows.some(
      (entry) => entry.point.clamped !== 0 || entry.point.suspiciousFast || entry.point.event !== null,
    )
    return (
      <div className="chart-tooltip">
        <div className="head">Lap {lapNumber}</div>
        {rows.map(({ item, point }) => (
          <div
            key={item.name}
            className={point.clamped !== 0 && point.event === null ? 'row is-outlier' : 'row'}
          >
            <span className="key-line" style={{ backgroundColor: item.color }} aria-hidden="true" />
            <span className="value">{formatClock(point.time)}</span>
            <span className="name">
              {item.name}
              {point.event !== null
                ? ` · ${EVENT_LABELS[point.event.kind].toLowerCase()} (${point.event.source === 'manual' ? 'human' : 'detector'})`
                : ''}
              {point.event === null && point.clamped === 1 ? ' · slow outlier' : ''}
              {point.event === null && point.clamped === -1 ? ' · off scale' : ''}
              {point.event === null && point.clamped === 0 && point.suspiciousFast
                ? ' · suspiciously fast'
                : ''}
            </span>
          </div>
        ))}
        {hasFlag && (
          <div className="note">
            Joker and pit laps are drawn as markers and left out of the line; outliers are pinned to
            the axis edge. The table view carries every raw value.
          </div>
        )}
      </div>
    )
  }

  const tableRows = model.lapNumbers

  return (
    <Card
      title={`Времена кругов · ${metric.label}`}
      caption="По линии на пилота. Вертикальная шкала подрезана по робастному диапазону (медиана ± k · MAD), чтобы аварийные круги не сплющили всё поле; вышедшие за него прижаты к краю треугольниками. Обязательные джокер (ромб) и пит (квадрат) отмечены там, где случились, и не участвуют ни в линии, ни в шкале."
      stale={stale}
      actions={
        <>
          <ViewToggle
            label="Обработка выбросов"
            value={scale}
            options={SCALE_OPTIONS}
            onChange={setScale}
          />
          <ViewToggle
            label="Вид: график или таблица"
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
          />
        </>
      }
    >
      {visible.length === 0 ? (
        <p className="empty">Все пилоты скрыты — включите кого-нибудь обратно в легенде.</p>
      ) : view === 'chart' ? (
        <>
          <div className="chart-frame">
            <ResponsiveContainer width="100%" height={360}>
              <LineChart data={model.rows} margin={{ top: 12, right: 20, bottom: 8, left: 4 }}>
                <CartesianGrid stroke={chrome.grid} strokeWidth={1} vertical={false} />
                <XAxis
                  dataKey="lap"
                  type="number"
                  domain={['dataMin', 'dataMax']}
                  ticks={model.lapNumbers.filter(
                    (lap) => model.lapNumbers.length <= 25 || lap % 5 === 0 || lap === 1,
                  )}
                  tick={{ fill: chrome.muted, fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: chrome.axis }}
                  label={{
                    value: 'Круг',
                    position: 'insideBottomRight',
                    offset: -4,
                    fill: chrome.muted,
                    fontSize: 11,
                  }}
                />
                <YAxis
                  type="number"
                  domain={model.domain}
                  ticks={model.ticks}
                  allowDataOverflow
                  width={76}
                  tickFormatter={formatClock}
                  tick={{ fill: chrome.muted, fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: chrome.axis }}
                />
                <Tooltip
                  content={renderTooltip}
                  cursor={{ stroke: chrome.axis, strokeWidth: 1 }}
                  isAnimationActive={false}
                />
                {visible.map((item) => (
                  <Line
                    key={item.name}
                    type="linear"
                    dataKey={seriesKey(item.slot)}
                    name={item.name}
                    stroke={item.color}
                    strokeWidth={2}
                    strokeLinejoin="round"
                    strokeLinecap="round"
                    dot={false}
                    activeDot={{
                      r: 4,
                      fill: item.color,
                      stroke: chrome.surface,
                      strokeWidth: 2,
                    }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
                {scale === 'clamp' &&
                  visible.map((item) => (
                    <Line
                      key={`${item.name}-high`}
                      type="linear"
                      dataKey={highKey(item.slot)}
                      name={`${item.name} (above scale)`}
                      stroke="none"
                      legendType="none"
                      tooltipType="none"
                      isAnimationActive={false}
                      dot={outlierMarker(item.color, chrome.surface, 1)}
                      activeDot={false}
                    />
                  ))}
                {scale === 'clamp' &&
                  visible.map((item) => (
                    <Line
                      key={`${item.name}-low`}
                      type="linear"
                      dataKey={lowKey(item.slot)}
                      name={`${item.name} (below scale)`}
                      stroke="none"
                      legendType="none"
                      tooltipType="none"
                      isAnimationActive={false}
                      dot={outlierMarker(item.color, chrome.surface, -1)}
                      activeDot={false}
                    />
                  ))}
                {visible.flatMap((item) =>
                  EVENT_MARK_KINDS.map((kind) => (
                    <Line
                      key={`${item.name}-${kind}`}
                      type="linear"
                      dataKey={eventKey(item.slot, kind)}
                      name={`${item.name} (${kind})`}
                      stroke="none"
                      legendType="none"
                      tooltipType="none"
                      isAnimationActive={false}
                      dot={eventMarker(item.color, chrome.surface, kind)}
                      activeDot={false}
                    />
                  )),
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <SeriesLegend
            series={series}
            hidden={hidden}
            onToggle={onToggle}
            onShowAll={onShowAll}
            keyShape="line"
            label="Пилоты на графике времён кругов"
          />
          <ul className="legend" aria-label="Обозначения событий круга">
            <li>
              <span className="static-key">
                <svg width={14} height={14} aria-hidden="true">
                  <path d="M 7 1 L 13 7 L 7 13 L 1 7 Z" fill={chrome.textSecondary} />
                </svg>
                Джокер
              </span>
            </li>
            <li>
              <span className="static-key">
                <svg width={14} height={14} aria-hidden="true">
                  <rect x={2} y={2} width={10} height={10} rx={1} fill={chrome.textSecondary} />
                </svg>
                Пит
              </span>
            </li>
          </ul>
          {model.events.length > 0 && (
            <p className="chart-footnote">
              {EVENT_MARK_KINDS.map((kind) => {
                const marked = model.events.filter((event) => event.kind === kind)
                if (marked.length === 0) return null
                return (
                  <span key={kind} className="event-run">
                    {EVENT_LABELS[kind]}:{' '}
                    {marked
                      .map((event) => `${event.driver} L${event.lapNumber}`)
                      .join(', ')}
                    .{' '}
                  </span>
                )
              })}
            </p>
          )}
          <p className="chart-footnote">
            Подписи оси — в формате mm:ss.mmm. Джокер и пит исключены и из шкалы, и из линии: это
            события, а не темп.{' '}
            {scale === 'clamp' && model.outliers > 0
              ? `Кругов за робастным диапазоном: ${model.outliers} — прижаты к краю оси треугольниками.`
              : null}
            {scale === 'hide' && model.outliers > 0
              ? `Кругов за робастным диапазоном: ${model.outliers} — скрыты, поэтому линии в этих местах разрываются.`
              : null}
            {scale === 'full'
              ? 'Полная шкала: один аварийный круг способен сплющить всё поле — включайте только чтобы рассмотреть инцидент.'
              : null}
          </p>
        </>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption>
              Значения в формате mm:ss.mmm. «!» — за робастным диапазоном (медиана ± {madK} · MAD),
              «*» — подозрительно быстрый круг, «J» — джокер, «P» — пит.
            </caption>
            <thead>
              <tr>
                <th className="left" scope="col">
                  Круг
                </th>
                {visible.map((item) => (
                  <th key={item.name} scope="col">
                    {item.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.map((lapNumber) => (
                <tr key={lapNumber}>
                  <th className="left" scope="row">
                    {lapNumber}
                  </th>
                  {visible.map((item) => {
                    const point = model.points.get(item.name)?.get(lapNumber)
                    const event = point?.event ?? null
                    return (
                      <td key={item.name}>
                        {formatClock(point?.time ?? null)}
                        {event !== null ? (event.kind === 'joker' ? ' J' : ' P') : ''}
                        {event === null && point?.clamped !== 0 && point?.time !== null ? ' !' : ''}
                        {event === null && point?.clamped === 0 && point.suspiciousFast ? ' *' : ''}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
