/**
 * The dashboard (SPEC section 7).
 *
 * One page per session: classification, the pace explorer (distribution,
 * rolling pace and raw lap times over a shared metric switch), the joker/pit
 * review panel, the pace metrics table and a two-driver comparison, all driven
 * by a single lap filter. Data comes from `createDataSource`, which falls back
 * to the bundled demo payload the first time the API turns out to be
 * unreachable, so the page is useful before the backend is up — with a banner
 * that says so. UI copy is Russian; code and comments stay English.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import type { EntryRow, SessionSummary } from './api'
import { DEFAULT_FILTER } from './api'
import { Card } from './components/Card'
import { ComparePanel } from './components/ComparePanel'
import { EventsPanel } from './components/EventsPanel'
import type { BoxSeries } from './components/BoxPlot'
import { ImportPanel } from './components/ImportPanel'
import { PaceExplorer } from './components/PaceExplorer'
import { PaceTable } from './components/PaceTable'
import { createDataSource } from './dataSource'
import type { EventKind } from './events'
import { buildEventModel, effectiveTagsOf } from './events'
import { usedLapsByDriver } from './metrics'
import { formatDuration, formatGap, formatSessionDate } from './format'
import { useAsync } from './hooks/useAsync'
import { useThemeMode } from './hooks/useThemeMode'
import { buildSeries } from './series'
import type { ThemePreference } from './theme'

const THEME_OPTIONS: readonly { value: ThemePreference; label: string }[] = [
  { value: 'system', label: 'Авто' },
  { value: 'light', label: 'Светлая' },
  { value: 'dark', label: 'Тёмная' },
]

function sessionLabel(session: SessionSummary): string {
  return session.code === null ? session.name : `${session.name} (${session.code})`
}

function ClassificationCard({ entries }: { entries: readonly EntryRow[] }) {
  return (
    <Card
      title="Классификация"
      caption="Как прислал тайминг. Колонка «Лучший круг» — официальная: как правило, это джокер, а не темп."
    >
      {entries.length === 0 ? (
        <p className="empty">У этой сессии нет строк классификации.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th className="left" scope="col">
                  Поз
                </th>
                <th scope="col">Карт</th>
                <th className="left" scope="col">
                  Пилот
                </th>
                <th scope="col">Круги</th>
                <th scope="col">Отставание</th>
                <th scope="col">Лучший круг</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.driver}>
                  <td className="left">{entry.position ?? '—'}</td>
                  <td>{entry.kart ?? '—'}</td>
                  <th className="left" scope="row">
                    {entry.driver}
                  </th>
                  <td>{entry.laps_count ?? '—'}</td>
                  <td>{formatGap(entry.gap_ms, entry.gap_laps)}</td>
                  <td className="numeric-strong">{formatDuration(entry.best_lap_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

export function App() {
  const theme = useThemeMode()
  const [offline, setOffline] = useState(false)
  // The filter bar was removed from the page: it sat above the charts, which it
  // does not affect, and read as broken. The defaults still drive /stats and
  // /compare (joker, pit and slow outliers excluded).
  const filter = DEFAULT_FILTER
  const [sessionId, setSessionId] = useState<number | null>(null)
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set())
  const [pair, setPair] = useState<{ a: string; b: string } | null>(null)
  // Bumped after a write (a lap tag, an import) to re-read everything derived.
  const [revision, setRevision] = useState(0)
  const [tagBusy, setTagBusy] = useState(false)
  const [tagError, setTagError] = useState<Error | null>(null)

  const goOffline = useCallback(() => {
    setOffline(true)
  }, [])
  // One failed request must not condemn the session to demo data for good: the
  // backend may simply have been starting up.
  const retryLive = useCallback(() => {
    setOffline(false)
    setRevision((current) => current + 1)
  }, [])
  const source = useMemo(() => createDataSource(offline, goOffline), [offline, goOffline])

  const sessions = useAsync((signal) => source.listSessions(signal), [source, revision])
  const list = sessions.data

  useEffect(() => {
    if (sessionId === null && list !== null && list.length > 0) setSessionId(list[0].id)
  }, [list, sessionId])

  const detail = useAsync(
    (signal) => source.getSession(sessionId as number, signal),
    [source, sessionId, revision],
    sessionId !== null,
  )
  const stats = useAsync(
    (signal) => source.getStats(sessionId as number, filter, signal),
    [source, sessionId, filter, revision],
    sessionId !== null,
  )
  const events = useAsync(
    (signal) => source.getEvents(sessionId as number, signal),
    [source, sessionId, revision],
    sessionId !== null,
  )

  const entries = useMemo(() => detail.data?.entries ?? [], [detail.data])
  const laps = useMemo(() => detail.data?.laps ?? [], [detail.data])
  const series = useMemo(() => buildSeries(entries, theme.mode), [entries, theme.mode])
  const driverNames = useMemo(() => series.map((item) => item.name), [series])

  const eventModel = useMemo(
    () => buildEventModel(laps, driverNames, events.data),
    [laps, driverNames, events.data],
  )

  const usedLaps = useMemo(() => usedLapsByDriver(stats.data?.drivers ?? null), [stats.data])

  const compareBoxes = useMemo<BoxSeries[]>(() => {
    if (pair === null) return []
    return [pair.a, pair.b]
      .map((name) => {
        const colour = series.find((item) => item.name === name)?.color ?? ''
        const used = usedLaps.get(name)
        const points = laps
          .filter((lap) => lap.driver === name && lap.time_ms !== null)
          .map((lap) => {
            const tags = effectiveTagsOf(lap)
            return {
              lap: lap.lap_number,
              value: lap.time_ms as number,
              tags,
              // Before /stats arrives, fall back to the tag test rather than
              // counting nothing — an empty box would read as "no data".
              counted:
                used === undefined
                  ? !tags.some((tag) => tag === 'joker' || tag === 'pit')
                  : used.has(lap.lap_number),
            }
          })
        return { name, color: colour, points }
      })
      .filter((item) => item.points.length > 0)
  }, [laps, series, pair, usedLaps])

  const mutateTag = useCallback((run: () => Promise<void>) => {
    setTagBusy(true)
    setTagError(null)
    run().then(
      () => {
        setTagBusy(false)
        setRevision((current) => current + 1)
      },
      (reason: unknown) => {
        setTagBusy(false)
        setTagError(reason instanceof Error ? reason : new Error(String(reason)))
      },
    )
  }, [])

  const onTag = useCallback(
    (lapId: number, tag: EventKind, note: string | null) => {
      mutateTag(() => source.addLapTag(lapId, tag, note))
    },
    [source, mutateTag],
  )
  const onUntag = useCallback(
    (lapId: number, tag: EventKind) => {
      mutateTag(() => source.removeLapTag(lapId, tag))
    },
    [source, mutateTag],
  )
  const onImported = useCallback(() => {
    setRevision((current) => current + 1)
  }, [])

  useEffect(() => {
    // Default pair: the two fastest drivers of the classification.
    setPair((current) => {
      if (driverNames.length < 2) return null
      if (current !== null && driverNames.includes(current.a) && driverNames.includes(current.b)) {
        return current
      }
      return { a: driverNames[0], b: driverNames[1] }
    })
  }, [driverNames])

  const comparison = useAsync(
    (signal) =>
      source.getComparison(
        sessionId as number,
        (pair as { a: string; b: string }).a,
        (pair as { a: string; b: string }).b,
        filter,
        signal,
      ),
    [source, sessionId, pair?.a, pair?.b, filter],
    sessionId !== null && pair !== null && pair.a !== pair.b,
  )

  const toggleDriver = useCallback((name: string) => {
    setHidden((current) => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])
  const showAll = useCallback(() => {
    setHidden(new Set())
  }, [])

  const current = list?.find((item) => item.id === sessionId) ?? null
  const loadError = sessions.error ?? detail.error ?? stats.error

  return (
    <div className="app">
      <header className="app-header">
        <div className="title-group">
          <h1>Pace Analysis</h1>
          <div className="subtitle">
            {current === null
              ? 'Темп картинговых гонок из писем Apex Timing'
              : `${sessionLabel(current)} · ${formatSessionDate(current.started_at)} · ${
                  current.track ?? 'трасса не указана'
                } · ${current.club ?? ''}`}
          </div>
        </div>
        <div className="segmented" role="group" aria-label="Цветовая тема">
          {THEME_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={theme.preference === option.value}
              onClick={() => {
                theme.setPreference(option.value)
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      </header>

      <div className="app-body">
        <div className="column">
          {offline && (
            <div className="banner" role="status">
              <span className="banner-icon" aria-hidden="true">
                ●
              </span>
              <div>
                <strong>Демо-данные</strong>
                <p>
                  API по адресу <code>/api</code> недоступен, поэтому страница показывает встроенный
                  снимок гонки Final A, а разметку кругов сохранить нельзя. Запустите бэкенд (
                  <code>make serve</code>) и повторите.
                </p>
                <button type="button" className="btn" onClick={retryLive}>
                  Повторить подключение
                </button>
              </div>
            </div>
          )}

          <Card
            title="Сессии"
            caption={list === null ? 'Загрузка…' : `сохранено: ${list.length}`}
            stale={sessions.loading}
          >
            {list === null || list.length === 0 ? (
              <p className="empty">
                {sessions.loading ? 'Загружаем сессии…' : 'Пока не импортировано ни одной сессии.'}
              </p>
            ) : (
              <ul className="session-list">
                {list.map((session) => (
                  <li key={session.id}>
                    <button
                      type="button"
                      className="session-item"
                      aria-current={session.id === sessionId}
                      onClick={() => {
                        setSessionId(session.id)
                        setHidden(new Set())
                      }}
                    >
                      <span className="name">{sessionLabel(session)}</span>
                      <span className="meta">
                        {formatSessionDate(session.started_at)} · {session.drivers_count} пилотов ·{' '}
                        {session.laps_count} кругов
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <ImportPanel
            onUpload={source.uploadEmails}
            onImported={onImported}
            offline={offline}
          />

          <ClassificationCard entries={entries} />
        </div>

        <div className="column">
          {loadError !== null && (
            <p className="error-note" role="alert">
              {loadError.message}
            </p>
          )}

          <PaceExplorer
            laps={laps}
            series={series}
            hidden={hidden}
            onToggle={toggleDriver}
            onShowAll={showAll}
            madK={filter.mad_k}
            usedLaps={usedLaps}
            eventsByLapId={eventModel.byLapId}
            mode={theme.mode}
            stale={detail.loading}
          />

          <PaceTable rows={stats.data?.drivers ?? []} series={series} stale={stats.loading} />

          <ComparePanel
            drivers={driverNames}
            driverA={pair?.a ?? ''}
            driverB={pair?.b ?? ''}
            onChange={(a, b) => {
              setPair({ a, b })
            }}
            comparison={comparison.data}
            error={comparison.error}
            boxes={compareBoxes}
            mode={theme.mode}
            stale={comparison.loading}
          />

          <EventsPanel
            model={eventModel}
            config={events.data?.config ?? null}
            busy={tagBusy}
            readOnly={offline}
            error={tagError ?? events.error}
            onTag={onTag}
            onUntag={onUntag}
            stale={detail.loading || events.loading}
          />
        </div>
      </div>
    </div>
  )
}
