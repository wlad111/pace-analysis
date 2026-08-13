import { useCallback, useId, useState } from 'react'
import type { DragEvent } from 'react'

import type { ImportReport, ImportStatus } from '../api'
import { Card } from './Card'

export interface ImportPanelProps {
  /** Uploads the files and returns one report per file. */
  onUpload: (files: File[]) => Promise<ImportReport[]>
  /** Called after an upload that changed the database, to refresh the page. */
  onImported: () => void
  /** The demo backend cannot store anything — say so instead of failing. */
  offline?: boolean
}

const STATUS_LABELS: Record<ImportStatus, string> = {
  imported: 'импортировано',
  merged: 'дополнено',
  already_imported: 'уже было',
  failed: 'ошибка',
}

function isEmail(file: File): boolean {
  return file.name.toLowerCase().endsWith('.eml') || file.type === 'message/rfc822'
}

/**
 * Upload block for Apex Timing result emails (`POST /api/imports`).
 *
 * Drag and drop or the file input, then one report per file: the same session
 * arriving twice is reported as "already imported" rather than as an error,
 * because the import is idempotent by design (SPEC §5).
 */
export function ImportPanel({ onUpload, onImported, offline = false }: ImportPanelProps) {
  const inputId = useId()
  const [over, setOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reports, setReports] = useState<ImportReport[] | null>(null)

  const submit = useCallback(
    (files: File[]) => {
      if (files.length === 0) return
      const emails = files.filter(isEmail)
      if (emails.length === 0) {
        setError('Импортировать можно только файлы .eml от Apex Timing.')
        setReports(null)
        return
      }
      setBusy(true)
      setError(null)
      onUpload(emails).then(
        (result) => {
          setReports(result)
          setBusy(false)
          if (result.some((report) => report.status !== 'failed')) onImported()
        },
        (reason: unknown) => {
          setReports(null)
          setBusy(false)
          setError(reason instanceof Error ? reason.message : String(reason))
        },
      )
    },
    [onUpload, onImported],
  )

  const onDrop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault()
    setOver(false)
    submit([...event.dataTransfer.files])
  }

  return (
    <Card title="Импорт писем" caption="Перетащите файлы .eml, которые Apex Timing присылает после гонки.">
      <div
        className={over ? 'dropzone is-over' : 'dropzone'}
        onDragOver={(event) => {
          event.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => {
          setOver(false)
        }}
        onDrop={onDrop}
      >
        <strong>{busy ? 'Импортируем…' : 'Перетащите сюда файлы .eml'}</strong>
        <span>or</span>
        <label className="btn" htmlFor={inputId}>
          Choose files
        </label>
        <input
          id={inputId}
          className="visually-hidden"
          type="file"
          accept=".eml,message/rfc822"
          multiple
          disabled={busy}
          onChange={(event) => {
            submit([...(event.target.files ?? [])])
            event.target.value = ''
          }}
        />
        <span className="hint">
          Повторно импортировать ту же гонку безопасно: сопоставление идёт по сессии, а не по
          идентификатору письма.
        </span>
      </div>

      {offline && (
        <p className="chart-footnote">
          API недоступен, поэтому загрузку некуда сохранить — запустите бэкенд
          (<code>make serve</code>).
        </p>
      )}

      {error !== null && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}

      {reports !== null &&
        (reports.length === 0 ? (
          <p className="empty">Сервер не принял ни одного файла.</p>
        ) : (
          <ul className="import-list">
            {reports.map((report) => (
              <li className="import-report" key={`${report.filename}-${report.detail}`}>
                <div className="import-head">
                  <span className="name">{report.filename}</span>
                  <span className={`badge is-${report.status}`}>
                    {STATUS_LABELS[report.status] ?? report.status}
                  </span>
                </div>
                <div className="detail">{report.detail}</div>
                {report.status !== 'failed' && (
                  <div className="hint">
                    сессия {report.session_id ?? '—'} · +{report.inserted_entries} участников · +
                    {report.inserted_laps} кругов · {report.updated_laps} дополнено секторами ·{' '}
                    размечено джокеров: {report.auto_jokers ?? 0}, питов: {report.auto_pits ?? 0}
                  </div>
                )}
                {(report.drivers_without_pit?.length ?? 0) > 0 && (
                  <div className="hint">
                    Пит не найден у: {report.drivers_without_pit?.join(', ')} — заезжать на пит
                    обязаны все, поэтому разметьте круг вручную в панели джокеров и питов.
                  </div>
                )}
                {report.conflicts.length > 0 && (
                  <ul>
                    {report.conflicts.map((conflict) => (
                      <li key={conflict}>{conflict}</li>
                    ))}
                  </ul>
                )}
                {report.warnings.length > 0 && (
                  <ul>
                    {report.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        ))}
    </Card>
  )
}
