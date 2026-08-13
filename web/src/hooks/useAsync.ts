import { useEffect, useState } from 'react'
import type { DependencyList } from 'react'

import { isAbortError } from '../api'

export interface AsyncState<T> {
  data: T | null
  error: Error | null
  /** True while a request is in flight; previous data stays on screen. */
  loading: boolean
}

/**
 * Runs `loader` whenever `deps` change, keeping the previous value visible
 * while the next one loads (no skeleton flash, no layout jump).
 */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  enabled = true,
): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ data: null, error: null, loading: enabled })

  useEffect(() => {
    if (!enabled) {
      setState((previous) => ({ ...previous, loading: false }))
      return
    }
    const controller = new AbortController()
    let active = true
    setState((previous) => ({ ...previous, loading: true }))
    loader(controller.signal).then(
      (value) => {
        if (active) setState({ data: value, error: null, loading: false })
      },
      (error: unknown) => {
        if (!active || isAbortError(error)) return
        setState((previous) => ({
          data: previous.data,
          error: error instanceof Error ? error : new Error(String(error)),
          loading: false,
        }))
      },
    )
    return () => {
      active = false
      controller.abort()
    }
    // The caller owns the dependency list; its length must stay constant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps])

  return state
}
