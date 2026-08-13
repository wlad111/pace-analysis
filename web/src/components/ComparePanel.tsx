import { useState } from 'react'

import type { DriverComparison } from '../api'
import { formatNumber, formatPValue, formatSignedDuration, formatSignedMillis } from '../format'
import type { ThemeMode } from '../theme'
import type { BoxSeries, BoxStatMode } from './BoxPlot'
import { BoxPlot } from './BoxPlot'
import { Card, ViewToggle } from './Card'

const STAT_OPTIONS = [
  { value: 'quantile', label: 'Медиана и квартили' },
  { value: 'moments', label: 'Среднее и SD' },
] as const

export interface ComparePanelProps {
  drivers: readonly string[]
  driverA: string
  driverB: string
  onChange: (a: string, b: string) => void
  comparison: DriverComparison | null
  error: Error | null
  /** Laps of the two selected drivers, for the side-by-side distributions. */
  boxes: readonly BoxSeries[]
  mode: ThemeMode
  stale?: boolean
}

const TEST_LABELS: Record<string, string> = {
  welch_t: 'Тест Уэлча (средние)',
  mann_whitney_u: 'U-критерий Манна — Уитни (ранги)',
  levene_brown_forsythe: 'Левене / Брауна — Форсайта (разброс)',
  bootstrap_median_diff: 'Бутстрэп-ДИ (разность медиан)',
}

function interval(low: number | null, high: number | null): string {
  if (low === null || high === null) return '—'
  return `[${formatNumber(low, 0)}, ${formatNumber(high, 0)}]`
}

/**
 * Head-to-head block: the two descriptive differences first, the tests after,
 * and the caveats last but never collapsed — laps of one race are not
 * independent draws, and the panel says so every single time (SPEC section 6).
 */
export function ComparePanel({
  drivers,
  driverA,
  driverB,
  onChange,
  comparison,
  error,
  boxes,
  mode,
  stale = false,
}: ComparePanelProps) {
  const [statMode, setStatMode] = useState<BoxStatMode>('quantile')
  return (
    <Card
      title="Head to head"
      caption="Разности ориентированы как A − B: положительное значение означает, что A медленнее."
      stale={stale}
      actions={
        <div className="compare-controls">
          <div className="field">
            <label htmlFor="compare-a">A</label>
            <select
              id="compare-a"
              value={driverA}
              onChange={(event) => {
                onChange(event.target.value, driverB)
              }}
            >
              {drivers.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="compare-b">B</label>
            <select
              id="compare-b"
              value={driverB}
              onChange={(event) => {
                onChange(driverA, event.target.value)
              }}
            >
              {drivers.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </div>
      }
    >
      {error !== null && <p className="error-note">{error.message}</p>}
      {comparison === null ? (
        <p className="empty">
          {error === null ? 'Выберите двух пилотов для сравнения.' : 'Для этой пары сравнение недоступно.'}
        </p>
      ) : (
        <>
          <div className="compare-boxes">
            <div className="explorer-bar">
              <span className="explorer-metric-label">Распределение темпа</span>
              <ViewToggle
                label="Что показывает ящик"
                value={statMode}
                options={STAT_OPTIONS}
                onChange={setStatMode}
              />
            </div>
            <BoxPlot
              series={boxes}
              statMode={statMode}
              mode={mode}
              height={400}
              slot={190}
              emptyText="Нет кругов для выбранной пары."
            />
          </div>

          <div className="stat-row">
            <div className="stat-tile">
              <div className="label">Разность медиан</div>
              <div className="value">{formatSignedDuration(comparison.median_diff_ms)}</div>
              <div className="hint">
                {comparison.driver_a} − {comparison.driver_b}, робастно
              </div>
            </div>
            <div className="stat-tile">
              <div className="label">Разность средних</div>
              <div className="value">{formatSignedDuration(comparison.mean_diff_ms)}</div>
              <div className="hint">чувствительна к отдельным кругам</div>
            </div>
            <div className="stat-tile">
              <div className="label">Зачётных кругов</div>
              <div className="value">
                {comparison.n_a} : {comparison.n_b}
              </div>
              <div className="hint">
                {comparison.driver_a} : {comparison.driver_b}
              </div>
            </div>
          </div>

          <div className="table-wrap">
            <table className="data">
              <caption>
                Statistic, p-value and 95% interval of each test. A “—” means the test could not be
                run; the reading explains why.
              </caption>
              <thead>
                <tr>
                  <th className="left" scope="col">
                    Тест
                  </th>
                  <th scope="col">Статистика</th>
                  <th scope="col">p</th>
                  <th scope="col">95% ДИ</th>
                  <th scope="col">Размер эффекта</th>
                </tr>
              </thead>
              <tbody>
                {comparison.tests.map((test) => (
                  <tr key={test.name}>
                    <th className="left" scope="row" title={test.interpretation}>
                      {TEST_LABELS[test.name] ?? test.name}
                    </th>
                    <td>{formatNumber(test.statistic, 2)}</td>
                    <td>{formatPValue(test.p_value)}</td>
                    <td>{interval(test.ci_low, test.ci_high)}</td>
                    <td>
                      {test.effect_size === null
                        ? '—'
                        : `${test.effect_name ?? 'effect'} ${formatSignedMillis(test.effect_size, 2)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul style={{ margin: '12px 0 0', paddingLeft: 18, fontSize: 12 }}>
            {comparison.tests.map((test) => (
              <li key={test.name} style={{ marginTop: 4 }}>
                {test.interpretation}
              </li>
            ))}
          </ul>

          <div className="caveats">
            <h3>Прочитайте это, прежде чем ссылаться на p-value</h3>
            <ul>
              {comparison.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          </div>
        </>
      )}
    </Card>
  )
}
