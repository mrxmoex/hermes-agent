/**
 * Shared `display.lease` / `display.status` ingest for every Bot Screen surface.
 *
 * The pane used to listen only to lease flips; a start/stop from CLI or another
 * Desktop never reached an already-open pane. Both surfaces now share this hook.
 *
 * Events that arrive before the first `display.status` has set `profile_key`
 * are buffered (connection-matched only) and replayed against the real key.
 * They are never applied without a key — two local profiles on one connection
 * would otherwise cross-paint.
 */

import { host, useValue } from '@hermes/plugin-sdk'
import type { RpcEvent } from '@hermes/plugin-sdk'
import { useEffect } from 'react'

import { botSelectionKey } from './data'
import {
  type DisplayLease,
  type DisplayStatus,
  isEventForBotScreen,
  isEventOnBotConnection
} from './screen-connection'
import { $screenState, screenStateFor, setScreenLease, setScreenStatus } from './screen-state'
import type { RosterRow } from './types'

const MAX_BUFFERED = 16
const pending = new Map<string, RpcEvent[]>()

export function resetScreenEventBufferForTests(): void {
  pending.clear()
}

export function applyScreenBackendEvent(
  bot: RosterRow,
  event: RpcEvent,
  profileKey: null | string | undefined
): boolean {
  if (!isEventForBotScreen(bot, event, profileKey)) {
    return false
  }

  if (event.type === 'display.lease') {
    const lease = (event.payload as { lease?: DisplayLease } | undefined)?.lease

    if (!lease) {
      return false
    }

    setScreenLease(bot, lease)

    return true
  }

  if (event.type === 'display.status') {
    const status = event.payload as DisplayStatus | undefined

    if (!status?.profile_key) {
      return false
    }

    setScreenStatus(bot, status)

    return true
  }

  return false
}

export function ingestScreenBackendEvent(
  bot: RosterRow,
  event: RpcEvent,
  profileKey: null | string | undefined
): void {
  if (applyScreenBackendEvent(bot, event, profileKey)) {
    return
  }

  if (profileKey || !isEventOnBotConnection(bot, event)) {
    return
  }

  const key = botSelectionKey(bot)
  const list = pending.get(key) ?? []

  list.push(event)

  if (list.length > MAX_BUFFERED) {
    list.shift()
  }

  pending.set(key, list)
}

export function flushScreenBackendEvents(bot: RosterRow, profileKey: null | string | undefined): void {
  if (!profileKey) {
    return
  }

  const key = botSelectionKey(bot)
  const list = pending.get(key)

  if (!list?.length) {
    return
  }

  pending.delete(key)

  for (const event of list) {
    applyScreenBackendEvent(bot, event, profileKey)
  }
}

/** Subscribe the current bot to backend screen pushes for the lifetime of the surface. */
export function useScreenBackendEvents(bot: RosterRow): void {
  const all = useValue($screenState)
  const profileKey = screenStateFor(all, bot)?.status?.profile_key

  useEffect(() => {
    flushScreenBackendEvents(bot, profileKey)
  }, [bot, profileKey])

  useEffect(() => {
    const onEvent = (event: RpcEvent) => ingestScreenBackendEvent(bot, event, profileKey)
    const offLease = host.onEvent('display.lease', onEvent)
    const offStatus = host.onEvent('display.status', onEvent)

    return () => {
      offLease()
      offStatus()
    }
  }, [bot, profileKey])
}
