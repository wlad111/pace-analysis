import { useCallback, useEffect, useState } from 'react'

import type { ThemeMode, ThemePreference } from '../theme'

const STORAGE_KEY = 'pace-analysis:theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

function readPreference(): ThemePreference {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : 'system'
}

function systemMode(): ThemeMode {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'
}

export interface ThemeControl {
  preference: ThemePreference
  mode: ThemeMode
  setPreference: (next: ThemePreference) => void
}

/**
 * Resolves the active colour mode. `prefers-color-scheme` is the default; an
 * explicit choice is stamped on `<html data-theme>` and wins over the OS in
 * both directions (see the palette reference).
 */
export function useThemeMode(): ThemeControl {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference)
  const [system, setSystem] = useState<ThemeMode>(systemMode)

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY)
    const onChange = (event: MediaQueryListEvent): void => {
      setSystem(event.matches ? 'dark' : 'light')
    }
    media.addEventListener('change', onChange)
    return () => {
      media.removeEventListener('change', onChange)
    }
  }, [])

  useEffect(() => {
    const root = document.documentElement
    if (preference === 'system') {
      delete root.dataset.theme
      window.localStorage.removeItem(STORAGE_KEY)
    } else {
      root.dataset.theme = preference
      window.localStorage.setItem(STORAGE_KEY, preference)
    }
  }, [preference])

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next)
  }, [])

  return {
    preference,
    mode: preference === 'system' ? system : preference,
    setPreference,
  }
}
