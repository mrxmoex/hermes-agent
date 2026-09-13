/**
 * Screen portal — the compact "this bot's computer" box that sits above a
 * bot's routines and on a gateway/profile group in the Sessions sidebar.
 * One click opens the live Screen pane; the subtitle says whether the screen
 * is live and who holds it, so the user knows before opening whether they
 * are about to watch, take over, install or start.
 *
 * Reads the same per-bot cache the pane paints (`$screenState`) and pulls
 * `display.status` until the cache settles (retrying transient RPC failures).
 */

import { Codicon, useValue } from '@hermes/plugin-sdk'
import type { ProfileGroupRoute } from '@hermes/plugin-sdk'
import { useCallback, useMemo } from 'react'

import { $lastRoster } from './data'
import { useBots } from './i18n'
import { resolveBotConnectionRoute } from './routing'
import { type DisplayLease, type DisplayStatus, leaseHeldBy, type ScreenViewer } from './screen-connection'
import { pullScreenStatus, useOnGatewayOpen, usePullScreenStatusUntilSettled, useScreenBackendEvents } from './screen-events'
import { openBotScreen } from './screen-open'
import { $screenState, screenStateFor } from './screen-state'
import type { BotMeta, RosterRow } from './types'

export type PortalTone = 'live' | 'handoff' | 'human' | 'other' | 'off' | 'missing' | 'unsupported' | 'unavailable' | 'unknown'

/** Pure: map cached status + lease (+ this window's minted viewer, if attached) to what the portal says. */
export function portalTone(status: DisplayStatus | null, lease: DisplayLease | null, viewer: ScreenViewer | null = null, unavailable = false): PortalTone {
  if (unavailable) {
    return 'unavailable'
  }

  if (!status) {
    return 'unknown'
  }

  if (!status.supported) {
    return 'unsupported'
  }

  if (!status.installed) {
    return 'missing'
  }

  // A leftover human lease / pending handoff outlives a crashed or stopped
  // launcher (dead Xvnc still fences computer_use). Surface that on the portal
  // so the user can open the pane and Hand back — do not hide it behind "off".
  if (lease?.holder === 'human') {
    return leaseHeldBy(lease, viewer) ? 'human' : 'other'
  }

  if (lease?.pending_handoff) {
    return 'handoff'
  }

  if (!status.running) {
    return 'off'
  }

  return 'live'
}

const TONE_ICON: Record<PortalTone, string> = {
  live: 'device-desktop',
  handoff: 'bell',
  human: 'record-keys',
  other: 'eye',
  off: 'debug-stop',
  missing: 'cloud-download',
  unsupported: 'circle-slash',
  unavailable: 'circle-slash',
  unknown: 'device-desktop'
}

const TONE_DOT: Record<PortalTone, string> = {
  live: 'bg-emerald-500',
  handoff: 'bg-amber-500',
  human: 'bg-red-500',
  other: 'bg-amber-500',
  off: 'bg-(--ui-text-quaternary)',
  missing: 'bg-(--ui-text-quaternary)',
  unsupported: 'bg-(--ui-text-quaternary)',
  unavailable: 'bg-(--ui-text-quaternary)',
  unknown: 'bg-(--ui-text-quaternary)'
}

export function useScreenPortalState(bot: RosterRow) {
  const all = useValue($screenState)
  const state = screenStateFor(all, bot)
  const status = state?.status ?? null

  useScreenBackendEvents(bot)
  usePullScreenStatusUntilSettled(bot)

  const resync = useCallback(() => {
    void pullScreenStatus(bot)
  }, [bot])

  useOnGatewayOpen(resync)

  return { status, lease: state?.lease ?? null, tone: portalTone(status, state?.lease ?? null, state?.viewer ?? null, state?.unavailable) }
}

export function ScreenPortal({ bot, meta, compact = false }: { bot: RosterRow; meta?: BotMeta | null; compact?: boolean }) {
  const t = useBots()
  const { status, tone } = useScreenPortalState(bot)

  const subtitle = {
    live: t.screen.portalWatching,
    handoff: t.screen.handoffRequested,
    human: t.screen.portalYouControl,
    other: t.screen.portalOtherControls,
    off: t.screen.portalStopped,
    missing: t.screen.portalNotInstalled,
    unsupported: t.screen.portalUnsupported,
    unavailable: t.screen.portalUnavailable,
    unknown: status?.display ?? ''
  }[tone]

  if ((tone === 'unsupported' || tone === 'unavailable') && compact) {
    return null
  }

  return (
    <button
      aria-label={`${t.screen.portalTitle}: ${subtitle}`}
      className="group flex w-full items-center gap-2 rounded-md border border-(--ui-stroke-secondary) bg-(--chrome-action-hover)/40 px-2 py-1.5 text-left transition-colors hover:bg-(--chrome-action-hover)"
      onClick={() => openBotScreen(bot, meta ?? null)}
      type="button"
    >
      <span className="relative grid size-7 shrink-0 place-items-center rounded bg-black/70 text-white/90">
        <Codicon name={TONE_ICON[tone]} />
        <span className={`absolute -right-0.5 -top-0.5 size-2 rounded-full ring-2 ring-(--ui-bg-primary) ${TONE_DOT[tone]}`} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium">{t.screen.portalTitle}</span>
        {subtitle ? <span className="block truncate text-[0.65rem] text-(--ui-text-tertiary)">{subtitle}</span> : null}
      </span>
      <span className="flex shrink-0 items-center gap-1 text-[0.65rem] text-(--ui-text-tertiary) opacity-0 transition-opacity group-hover:opacity-100">
        {t.screen.portalOpen} <Codicon name="arrow-right" />
      </span>
    </button>
  )
}

/** Sessions-sidebar variant: the gateway/profile group hands us its route; find the
 *  bot that owns it, or synthesize a scoped row so a profile outside the current
 *  roster filter still gets its portal (the portal only needs a routable row). */
export function ProfileGroupScreenPortal({ route }: { route: ProfileGroupRoute }) {
  const roster = useValue($lastRoster)
  const { connectionId, profile } = route

  // Stable row identity: the portal's effects key on `bot`, so a synthesized row
  // rebuilt every render would re-subscribe the lease listener on every sidebar paint.
  const bot = useMemo(
    () =>
      roster.find(row => {
        const resolved = resolveBotConnectionRoute(row)

        return resolved.route
          ? resolved.route.profile === profile && resolved.route.connectionId === (connectionId ?? 'local')
          : row.name === profile && connectionId === null
      }) ??
      (connectionId
        ? ({ name: profile, sourceScoped: true, connectionId, connectionKind: connectionId === 'local' ? 'local' : 'remote' } as RosterRow)
        : ({ name: profile } as RosterRow)),
    [connectionId, profile, roster]
  )

  return <ScreenPortal bot={bot} compact />
}
