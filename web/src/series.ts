import type { EntryRow } from './api'
import type { ThemeMode } from './theme'
import { SERIES_SLOTS, seriesColor } from './theme'

/** A driver plus the palette slot it owns for the whole page. */
export interface DriverSeries {
  name: string
  /** Stable slot from the classification order — never re-assigned on filter. */
  slot: number
  color: string
  /** True once the palette runs out of distinct hues (9th driver onwards). */
  isOther: boolean
}

export function buildSeries(entries: readonly EntryRow[], mode: ThemeMode): DriverSeries[] {
  return entries.map((entry, index) => ({
    name: entry.driver,
    slot: index,
    color: seriesColor(index, mode),
    isOther: index >= SERIES_SLOTS,
  }))
}
