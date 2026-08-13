/**
 * Chooses between the live API and the offline demo data.
 *
 * The dashboard starts optimistic: it calls the API, and the first
 * `ApiOfflineError` flips the whole page into demo mode (banner included)
 * without surfacing an error to the reader.
 *
 * Two rules keep that fallback honest. Only *reads* fall back — a write is
 * refused rather than faked, so a human verdict is never accepted into a void.
 * And a cancelled request is not an offline one: `api.send` re-throws
 * `AbortError` untouched, so switching sessions or filters mid-flight cannot
 * flip a perfectly healthy page into demo mode.
 */

import type {
  DriverComparison,
  EventReport,
  ImportReport,
  LapFilterState,
  RankingsResponse,
  SessionDetail,
  SessionSummary,
  StatsResponse,
  TagOption,
} from './api'
import {
  ApiOfflineError,
  addLapTag,
  getComparison,
  getEvents,
  getRankings,
  getSession,
  getStats,
  getTags,
  listSessions,
  removeLapTag,
  uploadEmails,
} from './api'
import {
  demoComparison,
  demoEvents,
  demoRankings,
  demoSession,
  demoSessions,
  demoStats,
  demoTags,
} from './mock/demo'

export interface DataSource {
  offline: boolean
  listSessions(signal?: AbortSignal): Promise<SessionSummary[]>
  getSession(id: number, signal?: AbortSignal): Promise<SessionDetail>
  getStats(id: number, filter: LapFilterState, signal?: AbortSignal): Promise<StatsResponse>
  getComparison(
    id: number,
    a: string,
    b: string,
    filter: LapFilterState,
    signal?: AbortSignal,
  ): Promise<DriverComparison>
  getTags(signal?: AbortSignal): Promise<TagOption[]>
  getRankings(id: number, signal?: AbortSignal): Promise<RankingsResponse>
  /** `null` when the backend does not serve the joker/pit report. */
  getEvents(id: number, signal?: AbortSignal): Promise<EventReport | null>
  addLapTag(lapId: number, tag: string, note?: string | null): Promise<void>
  removeLapTag(lapId: number, tag: string): Promise<void>
  uploadEmails(files: File[]): Promise<ImportReport[]>
}

/** Refused offline instead of being faked: a write must reach the database. */
class ReadOnlyDemoError extends Error {
  constructor(action: string) {
    super(`The API is offline — ${action} cannot be stored. Start the backend (make serve).`)
    this.name = 'ReadOnlyDemoError'
  }
}

export function createDataSource(offline: boolean, onOffline: () => void): DataSource {
  /** Reads may fall back to the bundled payload; the page still shows something. */
  async function guard<T>(live: () => Promise<T>, demo: () => Promise<T>): Promise<T> {
    if (offline) return demo()
    try {
      return await live()
    } catch (error) {
      if (error instanceof ApiOfflineError) {
        onOffline()
        return demo()
      }
      throw error
    }
  }

  /**
   * Writes may not. A lap tag is a human verdict that SPEC §10.3 declares
   * authoritative over the detector; accepting it into an in-memory stub would
   * show a stored decision that never left the browser.
   */
  async function write(action: string, live: () => Promise<void>): Promise<void> {
    if (offline) throw new ReadOnlyDemoError(action)
    try {
      await live()
    } catch (error) {
      if (error instanceof ApiOfflineError) onOffline()
      throw error
    }
  }

  return {
    offline,
    listSessions: (signal) => guard(() => listSessions(signal), () => demoSessions()),
    getSession: (id, signal) => guard(() => getSession(id, signal), () => demoSession(id)),
    getStats: (id, filter, signal) =>
      guard(() => getStats(id, filter, signal), () => demoStats(id, filter)),
    getComparison: (id, a, b, filter, signal) =>
      guard(() => getComparison(id, a, b, filter, signal), () => demoComparison(id, a, b, filter)),
    getTags: (signal) => guard(() => getTags(signal), () => demoTags()),
    getRankings: (id, signal) => guard(() => getRankings(id, signal), () => demoRankings(id)),
    getEvents: (id, signal) => guard(() => getEvents(id, signal), () => demoEvents(id)),
    addLapTag: (lapId, tag, note) =>
      write(`the ${tag} tag`, () => addLapTag(lapId, tag, note)),
    removeLapTag: (lapId, tag) =>
      write(`removing the ${tag} tag`, () => removeLapTag(lapId, tag)),
    // Importing cannot be faked either: it would have to parse an email and
    // store it. Offline, the panel says so instead of pretending.
    uploadEmails: async (files) => {
      if (offline) throw new Error('The API is offline — start the backend to import .eml files.')
      try {
        return await uploadEmails(files)
      } catch (error) {
        if (error instanceof ApiOfflineError) onOffline()
        throw error
      }
    },
  }
}
