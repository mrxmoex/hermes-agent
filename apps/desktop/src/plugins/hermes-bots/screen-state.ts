/**
 * Per-bot Bot Screen cache: backend truth (`display.status`) plus the last
 * `display.lease` event, keyed by the bot's roster identity. Renderer-owned
 * cache of backend state — never the authority.
 */

import { atom } from 'nanostores'

import { botSelectionKey } from './data'
import type { DisplayLease, DisplayStatus, ScreenViewer } from './screen-connection'
import type { RosterRow } from './types'

export interface BotScreenState {
  status: DisplayStatus | null
  lease: DisplayLease | null
  /** This window's server-minted identity for the bot's current attach; null until the pane observes. */
  viewer: ScreenViewer | null
  /** The bot's Hermes has no `display.*` methods (older backend): nothing to check, ever. */
  unavailable?: boolean
  /**
   * Bumped only when runtime status is written. An in-flight `display.status`
   * pull snapshots this and discards its reply if a live push landed first.
   * Lease / viewer writes do not move it — those have their own fences.
   */
  statusGen?: number
}

export const $screenState = atom<Record<string, BotScreenState>>({})

export function screenStateFor(all: Record<string, BotScreenState>, bot: RosterRow): BotScreenState | null {
  return all[botSelectionKey(bot)] ?? null
}

/** A lease whose epoch is below the one we hold is a slower response about the
 *  past (a `display.status` reply overtaken by a `display.lease` event). Payloads
 *  without an epoch — older backends — are always applied. */
function isOlderLease(prev: DisplayLease | null | undefined, next: DisplayLease): boolean {
  return typeof next.epoch === 'number' && typeof prev?.epoch === 'number' && next.epoch < prev.epoch
}

export function screenStatusGeneration(bot: RosterRow): number {
  return screenStateFor($screenState.get(), bot)?.statusGen ?? 0
}

export function setScreenStatus(bot: RosterRow, status: DisplayStatus): void {
  const key = botSelectionKey(bot)
  const current = $screenState.get()
  const prev = current[key]
  const lease = status.lease && !isOlderLease(prev?.lease, status.lease) ? status.lease : (prev?.lease ?? null)
  $screenState.set({
    ...current,
    [key]: { status, lease, viewer: prev?.viewer ?? null, statusGen: (prev?.statusGen ?? 0) + 1 }
  })
}

/** Apply a pull only if no live status write happened while it was in flight. */
export function applyScreenStatusIfUnchanged(bot: RosterRow, status: DisplayStatus, startedGen: number): boolean {
  if (screenStatusGeneration(bot) !== startedGen) {
    return false
  }

  setScreenStatus(bot, status)

  return true
}

/** `display.status` answered method-not-found: remember it so no surface keeps "checking". */
export function setScreenUnavailable(bot: RosterRow): void {
  const key = botSelectionKey(bot)
  const current = $screenState.get()
  const prev = current[key]

  if (prev?.unavailable) {
    return
  }

  $screenState.set({ ...current, [key]: { status: null, lease: null, viewer: null, unavailable: true } })
}

export function setScreenLease(bot: RosterRow, lease: DisplayLease): void {
  const key = botSelectionKey(bot)
  const current = $screenState.get()
  const prev = current[key]

  if (isOlderLease(prev?.lease, lease)) {
    return
  }

  if (
    prev?.lease &&
    prev.lease.holder === lease.holder &&
    prev.lease.viewer_id === lease.viewer_id &&
    prev.lease.viewer_hash === lease.viewer_hash &&
    prev.lease.pending_handoff === lease.pending_handoff &&
    prev.lease.reason === lease.reason &&
    prev.lease.since === lease.since &&
    prev.lease.epoch === lease.epoch
  ) {
    return
  }

  $screenState.set({
    ...current,
    [key]: { status: prev?.status ?? null, lease, viewer: prev?.viewer ?? null, statusGen: prev?.statusGen }
  })
}

/** Record the identity `display.observe` minted for this window's attach to `bot`. */
export function setScreenViewer(bot: RosterRow, viewer: ScreenViewer | null): void {
  const key = botSelectionKey(bot)
  const current = $screenState.get()
  const prev = current[key]

  if ((prev?.viewer ?? null) === viewer) {
    return
  }

  $screenState.set({
    ...current,
    [key]: { status: prev?.status ?? null, lease: prev?.lease ?? null, viewer, statusGen: prev?.statusGen }
  })
}
