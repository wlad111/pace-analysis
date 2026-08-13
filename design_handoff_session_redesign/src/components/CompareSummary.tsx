/**
 * Short head-to-head panel: the difference first, the tests underneath.
 *
 * Everything here comes from `GET /api/sessions/{id}/compare`, including the
 * caveats — the panel never recomputes statistics in the browser. Tests are
 * matched by name, and anything the server sends that this panel does not
 * recognise is still listed, so a new test in `karting.stats` shows up without
 * a frontend change.
 */

import type { DriverComparison, TestResult } from '../api'
import { formatDuration, formatPValue, formatSignedDuration } from '../format'
import { Card } from './Card'

export interface CompareSummaryProps {
  comparison: DriverComparison | null
  error?: Error | null
  stale?: boolean
}

/** Half-width of the CI bar in viewBox units, and the ms it represents. */
const BAR_HALF = 175
const BAR_SCALE_MS = 900

function findTest(tests: readonly TestResult[], pattern: RegExp): TestResult | undefined {
  return tests.find((test) => pattern.test(test.name))
}

export function CompareSummary({ comparison, error = null, stale = false }: CompareSummaryProps) {
  if (error !== null) {
    return (
      <Card title="Сравнение A / B">
        <p className="error-note" role="alert">
          {error.message}
        </p>
      </Card>
    )
  }
  if (comparison === null) {
    return (
      <Card title="Сравнение A / B" stale={stale}>
        <p className="empty">Выберите двух пилотов.</p>
      </Card>
    )
  }

  const { driver_a: a, driver_b: b, tests } = comparison
  const meanDiff = comparison.mean_diff_ms
  const medianDiff = comparison.median_diff_ms
  const bootstrap = findTest(tests, /bootstrap|median/i)
  const welch = findTest(tests, /welch|t-test/i)
  const mann = findTest(tests, /mann|whitney/i)
  const cliff = tests.find((test) => /cliff/i.test(test.effect_name ?? ''))
  const known = new Set([bootstrap, welch, mann, cliff].filter(Boolean))
  const ciLow = bootstrap?.ci_low ?? null
  const ciHigh = bootstrap?.ci_high ?? null
  const straddlesZero = ciLow !== null && ciHigh !== null && ciLow < 0 && ciHigh > 0

  const px = (ms: number): number =>
    190 + Math.max(-BAR_HALF, Math.min(BAR_HALF, (ms / BAR_SCALE_MS) * BAR_HALF))

  return (
    <Card
      title={`A / B: ${a} — ${b}`}
      caption={`n = ${comparison.n_a} / ${comparison.n_b} зачётных кругов`}
      stale={stale}
    >
      <div className="compare-headline">
        <b className={meanDiff !== null && meanDiff < 0 ? 'is-good' : undefined}>
          {formatSignedDuration(meanDiff)}
        </b>
        <span>с / круг · разность средних (A − B)</span>
      </div>

      {ciLow !== null && ciHigh !== null && medianDiff !== null && (
        <>
          <div className="compare-axis-labels">
            <span>← быстрее {a}</span>
            <span>быстрее {b} →</span>
          </div>
          <svg viewBox="0 0 380 42" width="100%" className="chart-frame" aria-hidden="true">
            <line x1="0" y1="22" x2="380" y2="22" stroke="var(--grid)" />
            <line x1="190" y1="2" x2="190" y2="34" stroke="var(--axis)" strokeDasharray="2 3" />
            <text x="190" y="41" textAnchor="middle" fontSize="9.5" fill="var(--text-muted)">
              0
            </text>
            <rect
              x={Math.min(px(ciLow), px(ciHigh))}
              y="12"
              width={Math.abs(px(ciHigh) - px(ciLow))}
              height="8"
              rx="4"
              fill="var(--accent)"
              fillOpacity="0.22"
            />
            <circle cx={px(medianDiff)} cy="16" r="5" fill="var(--accent)" />
          </svg>
          <p className="chart-footnote">
            95 % bootstrap-CI разности медиан: {formatSignedDuration(ciLow)} …{' '}
            {formatSignedDuration(ciHigh)} с.{' '}
            {straddlesZero
              ? 'Ноль внутри — порядок одной гонкой не установлен.'
              : 'Ноль вне интервала — разница устойчива в этой гонке.'}
          </p>
        </>
      )}

      <div className="stat-row">
        <div className="stat-tile">
          <div className="label">разность медиан</div>
          <div className="value">{formatSignedDuration(medianDiff)}</div>
          <div className="hint">
            {formatDuration(comparison.stats_a.median_ms)} против{' '}
            {formatDuration(comparison.stats_b.median_ms)}
          </div>
        </div>
        <div className="stat-tile">
          <div className="label">на дистанции</div>
          <div className="value">
            {meanDiff === null
              ? '—'
              : formatSignedDuration(meanDiff * Math.min(comparison.n_a, comparison.n_b))}
          </div>
          <div className="hint">за {Math.min(comparison.n_a, comparison.n_b)} зачётных кругов</div>
        </div>
        {welch !== undefined && (
          <div className="stat-tile">
            <div className="label">{welch.name}</div>
            <div className="value">p {formatPValue(welch.p_value)}</div>
            <div className="hint">t = {welch.statistic?.toFixed(2) ?? '—'}</div>
          </div>
        )}
        {(cliff ?? mann) !== undefined && (
          <div className="stat-tile">
            <div className="label">{(cliff ?? mann)?.effect_name ?? 'размер эффекта'}</div>
            <div className="value">{(cliff ?? mann)?.effect_size?.toFixed(2) ?? '—'}</div>
            <div className="hint">{(cliff ?? mann)?.interpretation}</div>
          </div>
        )}
      </div>

      {tests.filter((test) => !known.has(test)).length > 0 && (
        <table className="data">
          <tbody>
            {tests
              .filter((test) => !known.has(test))
              .map((test) => (
                <tr key={test.name}>
                  <th className="left" scope="row">
                    {test.name}
                  </th>
                  <td>{test.statistic?.toFixed(2) ?? '—'}</td>
                  <td className="muted">{formatPValue(test.p_value)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}

      {comparison.caveats.length > 0 && (
        <div className="caveats">
          <h3>Круги одной гонки не независимы</h3>
          <ul>
            {comparison.caveats.map((caveat) => (
              <li key={caveat}>{caveat}</li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}
