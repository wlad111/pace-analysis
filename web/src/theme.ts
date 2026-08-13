/**
 * Design tokens for the charts.
 *
 * The values come from the `dataviz` skill reference palette and were checked
 * with `scripts/validate_palette.js` for the six slots this dashboard can put
 * on screen at once:
 *   light  - all checks pass (contrast WARN on aqua/yellow/magenta, relieved by
 *            the always-available table views and the metrics table)
 *   dark   - all checks pass, every slot >= 3:1 on the dark surface
 * Slot order is the CVD-safety mechanism: never re-order, never cycle past 8.
 * CSS mirrors these values in `styles.css`; JS needs them because Recharts
 * paints SVG attributes, not CSS classes.
 */

export type ThemeMode = 'light' | 'dark'
export type ThemePreference = 'system' | 'light' | 'dark'

const SERIES_LIGHT = [
  '#2a78d6', // blue
  '#eb6834', // orange
  '#1baf7a', // aqua
  '#eda100', // yellow
  '#e87ba4', // magenta
  '#008300', // green
  '#4a3aa7', // violet
  '#e34948', // red
] as const

const SERIES_DARK = [
  '#3987e5',
  '#d95926',
  '#199e70',
  '#c98500',
  '#d55181',
  '#008300',
  '#9085e9',
  '#e66767',
] as const

export const SERIES_SLOTS = SERIES_LIGHT.length

export interface ChartChrome {
  surface: string
  plane: string
  textPrimary: string
  textSecondary: string
  muted: string
  grid: string
  axis: string
  border: string
  /** "Other" / de-emphasised series, and the ninth driver onwards. */
  deemphasis: string
  /** The emphasis hue: the one mark a chart is actually about. */
  accent: string
}

export const CHART_CHROME: Record<ThemeMode, ChartChrome> = {
  light: {
    surface: '#fcfcfb',
    plane: '#f9f9f7',
    textPrimary: '#0b0b0b',
    textSecondary: '#52514e',
    muted: '#898781',
    grid: '#e1e0d9',
    axis: '#c3c2b7',
    border: 'rgba(11,11,11,0.10)',
    deemphasis: '#898781',
    accent: '#2a78d6',
  },
  dark: {
    surface: '#1a1a19',
    plane: '#0d0d0d',
    textPrimary: '#ffffff',
    textSecondary: '#c3c2b7',
    muted: '#898781',
    grid: '#2c2c2a',
    axis: '#383835',
    border: 'rgba(255,255,255,0.10)',
    deemphasis: '#898781',
    accent: '#3987e5',
  },
}

/**
 * Colour of series `index` in `mode`. The index is the driver's stable slot
 * (classification order), so hiding or re-sorting series never repaints them.
 * Past the eighth slot the colour folds into the neutral "Other" grey instead
 * of generating a ninth hue.
 */
export function seriesColor(index: number, mode: ThemeMode): string {
  const slots = mode === 'dark' ? SERIES_DARK : SERIES_LIGHT
  return index >= 0 && index < slots.length ? slots[index] : CHART_CHROME[mode].deemphasis
}
