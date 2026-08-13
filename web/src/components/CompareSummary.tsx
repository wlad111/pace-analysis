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
  /**
   * Swap A and B.
   *
   * The ladder picks B; without this the subject A would stay whoever the
   * protocol put first, and no click could ever promote another driver.
   */
  onSwap?: () => void
  stale?: boolean
}

/** Half-width of the CI bar in viewBox units. */
const BAR_HALF = 175
/** Narrowest half-scale, so a 5 ms difference does not fill the whole bar. */
const MIN_SCALE_MS = 120

function findTest(tests: readonly TestResult[], pattern: RegExp): TestResult | undefined {
  return tests.find((test) => pattern.test(test.name))
}

export function CompareSummary({
  comparison,
  error = null,
  onSwap,
  stale = false,
}: CompareSummaryProps) {
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

  /**
   * Two intervals for two different estimands, kept apart on purpose.
   *
   * The headline is the mean difference, so the interval next to it has to be
   * the mean's: Welch's t interval, the same machinery that produced t and p.
   * The bootstrap interval belongs to the median and is drawn on its own row —
   * showing it under a "mean difference" headline was the confusion this panel
   * used to create.
   */
  const pairOf = (test: TestResult | undefined): [number, number] | null =>
    test?.ci_low === null || test?.ci_low === undefined || test.ci_high === null
      ? null
      : [test.ci_low, test.ci_high]
  const meanCi = pairOf(welch)
  const medianCi = pairOf(bootstrap)
  const straddlesZero = meanCi !== null && meanCi[0] < 0 && meanCi[1] > 0

  // The scale follows the data: a fixed +-900 ms would squash two evenly
  // matched drivers into a dot, and clip a blow-out.
  const extent = Math.max(
    MIN_SCALE_MS,
    ...[meanCi?.[0], meanCi?.[1], medianCi?.[0], medianCi?.[1], meanDiff, medianDiff]
      .filter((value): value is number => typeof value === 'number')
      .map((value) => Math.abs(value) * 1.15),
  )
  const px = (ms: number): number =>
    190 + Math.max(-BAR_HALF, Math.min(BAR_HALF, (ms / extent) * BAR_HALF))

  return (
    <Card
      title={`A / B: ${a} — ${b}`}
      caption={`n = ${comparison.n_a} / ${comparison.n_b} зачётных кругов`}
      stale={stale}
      actions={
        onSwap === undefined ? undefined : (
          <button type="button" className="btn" onClick={onSwap} title="Поменять A и B местами">
            A ⇄ B
          </button>
        )
      }
    >
      <div className="compare-headline">
        <b className={meanDiff !== null && meanDiff < 0 ? 'is-good' : undefined}>
          {formatSignedDuration(meanDiff)}
        </b>
        <span>с / круг · разность средних (A − B)</span>
      </div>

      {(meanCi !== null || medianCi !== null) && (
        <>
          <div className="compare-axis-labels">
            <span>← быстрее {a}</span>
            <span>быстрее {b} →</span>
          </div>
          <svg viewBox="0 0 380 76" width="100%" className="chart-frame" role="img">
            <line x1="0" y1="20" x2="380" y2="20" stroke="var(--grid)" />
            <line x1="0" y1="52" x2="380" y2="52" stroke="var(--grid)" />
            <line x1="190" y1="4" x2="190" y2="64" stroke="var(--axis)" strokeDasharray="2 3" />
            <text x="190" y="73" textAnchor="middle" fontSize="9.5" fill="var(--text-muted)">
              0
            </text>
            {meanCi !== null && meanDiff !== null && (
              <g>
                <rect
                  x={Math.min(px(meanCi[0]), px(meanCi[1]))}
                  y="12"
                  width={Math.max(2, Math.abs(px(meanCi[1]) - px(meanCi[0])))}
                  height="9"
                  rx="4"
                  fill="var(--accent)"
                  fillOpacity="0.24"
                />
                <circle cx={px(meanDiff)} cy="16.5" r="5" fill="var(--accent)" />
                <title>
                  {`Разность средних ${formatSignedDuration(meanDiff)} с, 95% ДИ по Уэлчу ` +
                    `[${formatSignedDuration(meanCi[0])}, ${formatSignedDuration(meanCi[1])}] с. ` +
                    `Интервал t-распределения по выборочным дисперсиям, без предположения об их равенстве.`}
                </title>
              </g>
            )}
            {medianCi !== null && medianDiff !== null && (
              <g>
                <rect
                  x={Math.min(px(medianCi[0]), px(medianCi[1]))}
                  y="44"
                  width={Math.max(2, Math.abs(px(medianCi[1]) - px(medianCi[0])))}
                  height="9"
                  rx="4"
                  fill="var(--text-secondary)"
                  fillOpacity="0.20"
                />
                <circle cx={px(medianDiff)} cy="48.5" r="4.5" fill="var(--text-secondary)" />
                <title>
                  {`Разность медиан ${formatSignedDuration(medianDiff)} с, 95% перцентильный ` +
                    `бутстрэп [${formatSignedDuration(medianCi[0])}, ${formatSignedDuration(medianCi[1])}] с. ` +
                    `Медиана не имеет удобной формулы дисперсии, поэтому интервал получен ресэмплингом.`}
                </title>
              </g>
            )}
            <text x="2" y="10" fontSize="9.5" fill="var(--accent)">
              средние · t Уэлча
            </text>
            <text x="2" y="42" fontSize="9.5" fill="var(--text-muted)">
              медианы · бутстрэп
            </text>
          </svg>
          <p className="chart-footnote">
            Две разные оценки одной пары, поэтому и интервалы разные.{' '}
            <b>Средние</b>: {formatSignedDuration(meanDiff)} с, 95% ДИ{' '}
            {meanCi === null
              ? '—'
              : `${formatSignedDuration(meanCi[0])} … ${formatSignedDuration(meanCi[1])}`}{' '}
            — интервал Стьюдента по выборочным дисперсиям (приближение ЦПТ, дисперсии не
            предполагаются равными). Это тот же аппарат, что даёт t и p выше, поэтому средние,
            интервал и тест согласованы между собой.{' '}
            <b>Медианы</b>: {formatSignedDuration(medianDiff)} с, 95% ДИ{' '}
            {medianCi === null
              ? '—'
              : `${formatSignedDuration(medianCi[0])} … ${formatSignedDuration(medianCi[1])}`}{' '}
            — перцентильный бутстрэп: у медианы нет простой формулы для дисперсии, и ЦПТ к ней
            напрямую не применяется.{' '}
            {straddlesZero
              ? 'Интервал средних накрывает ноль — порядок одной гонкой не установлен.'
              : 'Интервал средних не накрывает ноль — в этой гонке разница устойчива.'}{' '}
            Наведите курсор на полосу — там метод и границы.
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
