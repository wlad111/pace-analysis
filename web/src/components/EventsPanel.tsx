import { useState } from 'react'

import type { DetectedEvent, EventConfigInfo, LapRow } from '../api'
import type { DriverEventRow, EventKind, EventModel, LapEvent } from '../events'
import { EVENT_LABELS, EVENT_KINDS } from '../events'
import { formatDuration, formatNumber, formatPercent, formatSignedDuration } from '../format'
import { Card } from './Card'

export interface EventsPanelProps {
  model: EventModel
  /** Detector thresholds echoed by the API, when it ran. */
  config: EventConfigInfo | null
  /** A tag change is in flight — every button is disabled meanwhile. */
  busy?: boolean
  /** Demo data: writes cannot reach the database, so they are not offered. */
  readOnly?: boolean
  error?: Error | null
  onTag: (lapId: number, tag: EventKind, note: string | null) => void
  onUntag: (lapId: number, tag: EventKind) => void
  stale?: boolean
}

function sectorLabel(index: number | null): string {
  return index === null ? '—' : `S${index + 1}`
}

function sourceBadge(source: 'auto' | 'manual') {
  return (
    <span className={source === 'manual' ? 'badge is-manual' : 'badge is-auto'}>
      {source === 'manual' ? 'human' : 'detector'}
    </span>
  )
}

function lapOptionLabel(lap: LapRow): string {
  return `Lap ${lap.lap_number} — ${formatDuration(lap.time_ms)}`
}

/**
 * What the detector has to say about a lap that is not tagged.
 *
 * A proposal with zero confidence is not a detection: it is the slowest lap the
 * detector offers because the pit stop is mandatory and it found none, so it
 * must not be worded as a finding (SPEC section 10.2).
 */
function suggestionText(event: DetectedEvent): string {
  const label = EVENT_LABELS[event.kind === 'joker' ? 'joker' : 'pit']
  const parts = [`lap ${event.lap_number}`]
  if (event.ratio !== null) parts.push(`ratio ${formatNumber(event.ratio, 3)}`)
  if (!event.confidence) {
    return `no ${label.toLowerCase()} found — slowest lap is ${parts.join(', ')}`
  }
  parts.push(`confidence ${formatPercent(event.confidence, 0)}`)
  return `detector proposed ${label}: ${parts.join(', ')}, not in force`
}

/**
 * Joker / pit review (SPEC §10.2, §10.3).
 *
 * The detector proposes, a human disposes: every automatic tag can be confirmed
 * or removed, every driver without a full set gets an explicit invitation to
 * annotate the lap by hand, and the badge always says which of the two put the
 * tag there — a manual annotation voids the automatic ones for that lap.
 */
export function EventsPanel({
  model,
  config,
  busy = false,
  readOnly = false,
  error = null,
  onTag,
  onUntag,
  stale = false,
}: EventsPanelProps) {
  const [choice, setChoice] = useState<Record<string, number>>({})
  // A verdict that cannot be stored must not look like one that was.
  const locked = busy || readOnly

  const missing = model.rows.filter(
    (row) => row.joker === null || row.pit === null || row.suggestions.length > 0,
  )
  const events: LapEvent[] = model.events

  const pick = (row: DriverEventRow, kind: EventKind): number | null => {
    const key = `${row.driver}:${kind}`
    const chosen = choice[key]
    if (chosen !== undefined) return chosen
    const fallback = row.suggestions.find((event) => event.kind === kind)
    const byNumber = row.laps.find((lap) => lap.lap_number === fallback?.lap_number)
    if (byNumber !== undefined) return byNumber.id
    return row.laps.find((lap) => lap.time_ms !== null)?.id ?? null
  }

  return (
    <Card
      title="Джокеры и питы"
      caption={
        config === null
          ? 'По одному джокеру и одному питу на пилота, и ни один из них не является темпом. Ниже — действующая разметка; решение человека всегда главнее детектора.'
          : `Определяется по отношению к собственной робастной базе пилота: пит — не меньше ${formatNumber(config.pit_ratio, 2)} × базы, джокер — не больше ${formatNumber(config.joker_ratio, 2)} ×. Решение человека всегда главнее детектора.`
      }
      stale={stale}
    >
      <div className="stat-row">
        <div className="stat-tile">
          <div className="label">Джокеров размечено</div>
          <div className="value">
            {model.jokers} из {model.rows.length}
          </div>
          <div className="hint">ожидается по одному на пилота</div>
        </div>
        <div className="stat-tile">
          <div className="label">Питов размечено</div>
          <div className="value">
            {model.pits} из {model.rows.length}
          </div>
          <div className="hint">ожидается по одному на пилота</div>
        </div>
        <div className="stat-tile">
          <div className="label">Поставлено вручную</div>
          <div className="value">{model.manual}</div>
          <div className="hint">the rest come from the detector</div>
        </div>
      </div>

      {readOnly && (
        <p className="error-note" role="status">
          This page is showing bundled demo data, so joker and pit verdicts cannot be stored. Start
          the backend (<code>make serve</code>) and reload to annotate laps.
        </p>
      )}

      {error !== null && (
        <p className="error-note" role="alert">
          {error.message}
        </p>
      )}

      {events.length === 0 ? (
        <p className="empty">
          No joker or pit lap is tagged in this session — tag them below, or re-run the import so
          the detector can propose them.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption>
              Ratio and Δ compare the lap with the driver&apos;s own robust baseline (their median
              lap once the start lap and both events are removed). A “—” means the detector did not
              supply that number for this lap.
            </caption>
            <thead>
              <tr>
                <th className="left" scope="col">
                  Driver
                </th>
                <th className="left" scope="col">
                  Event
                </th>
                <th scope="col">Круг</th>
                <th scope="col">Время</th>
                <th scope="col">Отношение</th>
                <th scope="col">Δ к базе</th>
                <th scope="col">Сектор</th>
                <th scope="col">Уверенность</th>
                <th scope="col">Источник</th>
                <th className="left" scope="col">
                  Действия
                </th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={`${event.lapId}-${event.kind}`}>
                  <th className="left" scope="row">
                    {event.driver}
                  </th>
                  <td className="left">{EVENT_LABELS[event.kind]}</td>
                  <td>{event.lapNumber}</td>
                  <td className="numeric-strong">{formatDuration(event.timeMs)}</td>
                  <td>{formatNumber(event.detection?.ratio ?? null, 3)}</td>
                  <td>{formatSignedDuration(event.detection?.delta_ms ?? null)}</td>
                  <td>{sectorLabel(event.detection?.sector_index ?? null)}</td>
                  <td>{formatPercent(event.detection?.confidence ?? null, 0)}</td>
                  <td title={event.note ?? undefined}>{sourceBadge(event.source)}</td>
                  <td className="left">
                    <span className="row-actions">
                      {event.source === 'auto' && (
                        <button
                          type="button"
                          className="btn"
                          disabled={locked}
                          title="Record that a human agrees with the detector"
                          onClick={() => {
                            onTag(event.lapId, event.kind, 'confirmed from the dashboard')
                          }}
                        >
                          Confirm
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn"
                        disabled={locked}
                        title={`Снять тег «${EVENT_LABELS[event.kind]}» с круга ${event.lapNumber}`}
                        onClick={() => {
                          onUntag(event.lapId, event.kind)
                        }}
                      >
                        Снять
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {missing.length > 0 && (
        <div className="annotate">
          <h3>Требуется решение человека</h3>
          <p className="caption">
            Формат требует ровно одного джокера и одного пита на пилота. Там, где детектор ничего
            не нашёл — джокер, проеханный с ошибкой, выглядит как обычный круг, — выберите круг
            сами. Чтобы перенести событие на другой круг, снимите его в таблице выше и задайте здесь.
          </p>
          <ul className="annotate-list">
            {missing.map((row) => (
              <li key={row.driver}>
                <div className="annotate-head">
                  <strong>{row.driver}</strong>
                  {row.suggestions.map((event) => (
                    <span key={`${event.kind}-${event.lap_number}`} className="hint">
                      {suggestionText(event)}
                    </span>
                  ))}
                </div>
                <div className="annotate-controls">
                  {EVENT_KINDS.filter((kind) => row[kind] === null).map((kind) => {
                    const id = `assign-${kind}-${row.driver}`
                    const selected = pick(row, kind)
                    return (
                      <span className="field" key={kind}>
                        <label htmlFor={id}>{EVENT_LABELS[kind]} lap</label>
                        <select
                          id={id}
                          value={selected ?? ''}
                          disabled={locked || row.laps.length === 0}
                          onChange={(event) => {
                            const value = Number(event.target.value)
                            setChoice((current) => ({ ...current, [`${row.driver}:${kind}`]: value }))
                          }}
                        >
                          {row.laps
                            .filter((lap) => lap.time_ms !== null)
                            .map((lap) => (
                              <option key={lap.id} value={lap.id}>
                                {lapOptionLabel(lap)}
                              </option>
                            ))}
                        </select>
                        <button
                          type="button"
                          className="btn primary"
                          disabled={locked || selected === null}
                          onClick={() => {
                            if (selected !== null) onTag(selected, kind, 'set from the dashboard')
                          }}
                        >
                          Tag as {kind}
                        </button>
                      </span>
                    )
                  })}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(model.warnings.length > 0 || !model.detectorAvailable) && (
        <div className="caveats">
          <h3>Замечания детектора</h3>
          <ul>
            {!model.detectorAvailable && (
              <li>
                Этот бэкенд не отдаёт <code>/api/sessions/&#123;id&#125;/events</code>, поэтому
                отношения и уверенность неизвестны. Ниже показана та разметка, что сохранена на кругах.
              </li>
            )}
            {model.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}
