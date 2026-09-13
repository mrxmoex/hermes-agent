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
 *
 * Buffered `display.status` is a miss signal, not a snapshot to replay: a
 * stale push (or one that survived unmount) must not overwrite a newer RPC
 * pull. Flush applies leases (epoch-fenced) and re-pulls if a status was
 * queued. Live status after the key is known still applies immediately.
 */

import { host, useValue } from '@hermes/plugin-sdk'
import type { RpcEvent } from '@hermes/plugin-sdk'
import { useEffect, useRef } from 'react'

import { botSelectionKey } from './data'
import {
  type DisplayLease,
  type DisplayStatus,
  displayRequest,
  isDisplayUnavailable,
  isEventForBotScreen,
  isEventOnBotConnection
} from './screen-connection'
import {
  $screenState,
  applyScreenStatusIfUnchanged,
  screenStateFor,
  screenStatusGeneration,
  setScreenLease,
  setScreenStatus,
  setScreenUnavailable
} from './screen-state'
import type { RosterRow } from './types'

const MAX_BUFFERED = 16
const pending = new Map<string, RpcEvent[]>()

export function resetScreenEventBufferForTests(): void {
  pending.clear()
}

export type PullScreenStatusResult = 'ok' | 'unavailable' | 'failed'

/** Delay between transient `display.status` failures while a surface is still unsettled. */
export const SCREEN_STATUS_RETRY_MS = 2000

/** One `display.status` RPC into the per-bot cache. Offline stays as-is; method-not-found settles unavailable. */
export async function pullScreenStatus(bot: RosterRow): Promise<PullScreenStatusResult> {
  const started = screenStatusGeneration(bot)

  try {
    applyScreenStatusIfUnchanged(bot, await displayRequest<DisplayStatus>(bot, 'display.status'), started)

    return 'ok'
  } catch (error) {
    if (isDisplayUnavailable(error)) {
      setScreenUnavailable(bot)

      return 'unavailable'
    }

    return 'failed'
  }
}

/**
 * Pull until the cache has a status or a method-not-found. A 502 / timeout
 * used to leave the portal on `'unknown'` forever (the mount effect saw
 * `status == null` and did not run again). Retry only while the gateway is up;
 * reconnect is a separate rising edge (`useOnGatewayOpen`).
 */
export function usePullScreenStatusUntilSettled(bot: RosterRow): void {
  const all = useValue($screenState)
  const state = screenStateFor(all, bot)
  const settled = Boolean(state?.status || state?.unavailable)
  const gatewayUp = useValue(host.state.gateway) === 'open'

  useEffect(() => {
    if (settled || !gatewayUp) {
      return
    }

    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const pull = () => {
      void pullScreenStatus(bot).then(result => {
        if (cancelled || result !== 'failed') {
          return
        }

        timer = setTimeout(pull, SCREEN_STATUS_RETRY_MS)
      })
    }

    pull()

    return () => {
      cancelled = true

      if (timer !== undefined) {
        clearTimeout(timer)
      }
    }
  }, [bot, gatewayUp, settled])
}

/** Run `callback` when the gateway socket becomes `open` (SSH reconnect, sleep/wake). */
export function useOnGatewayOpen(callback: () => void): void {
  const gatewayUp = useValue(host.state.gateway) === 'open'
  const wasUp = useRef(gatewayUp)

  useEffect(() => {
    const rose = gatewayUp && !wasUp.current
    wasUp.current = gatewayUp

    if (rose) {
      callback()
    }
  }, [callback, gatewayUp])
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

/** Replay buffered events. Returns true when a status was queued and must be re-pulled. */
export function flushScreenBackendEvents(bot: RosterRow, profileKey: null | string | undefined): boolean {
  if (!profileKey) {
    return false
  }

  const key = botSelectionKey(bot)
  const list = pending.get(key)

  if (!list?.length) {
    return false
  }

  pending.delete(key)
  let discardedStatus = false

  for (const event of list) {
    if (event.type === 'display.status') {
      discardedStatus = true
      continue
    }

    applyScreenBackendEvent(bot, event, profileKey)
  }

  return discardedStatus
}

/** Subscribe the current bot to backend screen pushes for the lifetime of the surface. */
export function useScreenBackendEvents(bot: RosterRow): void {
  const all = useValue($screenState)
  const profileKey = screenStateFor(all, bot)?.status?.profile_key

  useEffect(() => {
    if (flushScreenBackendEvents(bot, profileKey)) {
      void pullScreenStatus(bot)
    }
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
