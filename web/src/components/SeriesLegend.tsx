import type { DriverSeries } from '../series'

export interface SeriesLegendProps {
  series: readonly DriverSeries[]
  hidden: ReadonlySet<string>
  onToggle: (name: string) => void
  onShowAll: () => void
  /** Legends mirror the mark: a line key for lines, a swatch for boxes/bars. */
  keyShape?: 'line' | 'rect'
  label?: string
}

/**
 * Always-present identity channel. Clicking a key hides or shows the series;
 * the colour belongs to the driver, so hiding one never repaints the others.
 */
export function SeriesLegend({
  series,
  hidden,
  onToggle,
  onShowAll,
  keyShape = 'line',
  label = 'Пилоты',
}: SeriesLegendProps) {
  const anyHidden = series.some((item) => hidden.has(item.name))
  return (
    <ul className="legend" aria-label={label}>
      {series.map((item) => {
        const visible = !hidden.has(item.name)
        return (
          <li key={item.name}>
            <button
              type="button"
              aria-pressed={visible}
              onClick={() => {
                onToggle(item.name)
              }}
              title={visible ? `Скрыть ${item.name}` : `Показать ${item.name}`}
            >
              <span
                className={keyShape === 'line' ? 'key-line' : 'key-rect'}
                style={{ backgroundColor: item.color }}
                aria-hidden="true"
              />
              {item.name}
            </button>
          </li>
        )
      })}
      {anyHidden && (
        <li>
          <button type="button" onClick={onShowAll}>
            Показать всех
          </button>
        </li>
      )}
    </ul>
  )
}
