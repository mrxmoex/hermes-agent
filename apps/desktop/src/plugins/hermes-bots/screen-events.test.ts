/**
 * Screen event ingest: apply only when connection + profile_key match;
 * buffer connection-matched events until the first status sets the key;
 * never apply a sibling profile's payload on the same connection;
 * never let a buffered display.status overwrite a newer pull.
 */

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

vi.mock('@hermes/plugin-sdk', async () => {
  const { useStore } = await import('@nanostores/react')
  const { onGatewayEvent } = await import('../../contrib/events')

  return {
    useValue: useStore,
    host: { onEvent: onGatewayEvent, requestProfile: vi.fn() },
    resolveSiblingWsUrl: vi.fn()
  }
})
vi.mock('./data', () => ({
  botSelectionKey: (bot: RosterRow) => `${bot.connectionId ?? 'local'}:${bot.name}`
}))
vi.mock('./routing', () => ({
  botConnectionRoute: (bot: RosterRow) =>
    bot.connectionId ? { connectionId: bot.connectionId, profile: bot.name } : { connectionId: 'local', profile: bot.name }
}))

import { host } from '@hermes/plugin-sdk'
import { emitGatewayEvent } from '../../contrib/events'
import {
  flushScreenBackendEvents,
  ingestScreenBackendEvent,
  resetScreenEventBufferForTests,
  useScreenBackendEvents
} from './screen-events'
import type { DisplayLease, DisplayStatus } from './screen-connection'
import { $screenState, screenStateFor, setScreenStatus } from './screen-state'

const bot: RosterRow = { name: 'ops', sourceScoped: true, connectionId: 'host-a', connectionKind: 'remote' }
const key = '/home/hermes/.hermes'

const agent: DisplayLease = {
  holder: 'agent',
  viewer_id: null,
  viewer_hash: null,
  pending_handoff: null,
  since: 1,
  reason: '',
  epoch: 1
}

const stopped: DisplayStatus = {
  profile: 'ops',
  profile_key: key,
  supported: true,
  installed: true,
  missing: [],
  running: false,
  pid: null,
  display: null,
  socket: null,
  geometry: '1440x900',
  install_command: null,
  lease: agent
}

const running: DisplayStatus = { ...stopped, running: true, pid: 42, display: ':20', socket: '/tmp/rfb.sock' }
const human: DisplayLease = { ...agent, holder: 'human', viewer_hash: 'abc123abc123', epoch: 2, reason: 'Sign in' }

beforeEach(() => {
  $screenState.set({})
  resetScreenEventBufferForTests()
})

afterEach(() => {
  resetScreenEventBufferForTests()
})

describe('ingestScreenBackendEvent', () => {
  it('applies a display.status push once the profile key is known', () => {
    setScreenStatus(bot, stopped)
    ingestScreenBackendEvent(
      bot,
      { type: 'display.status', connectionId: 'host-a', payload: running },
      key
    )
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBe(true)
  })

  it('does not apply a same-path status from another host', () => {
    setScreenStatus(bot, stopped)
    ingestScreenBackendEvent(
      bot,
      { type: 'display.status', connectionId: 'host-b', payload: running },
      key
    )
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBe(false)
  })

  it('does not let a buffered display.status overwrite a newer pull', () => {
    ingestScreenBackendEvent(bot, { type: 'display.status', connectionId: 'host-a', payload: running }, null)
    setScreenStatus(bot, stopped)
    expect(flushScreenBackendEvents(bot, key)).toBe(true)
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBe(false)
  })

  it('replays a lease that arrived before the first status, and drops a sibling profile on the same connection', () => {
    ingestScreenBackendEvent(
      bot,
      {
        type: 'display.lease',
        connectionId: 'host-a',
        payload: { profile_key: key, lease: human }
      },
      null
    )
    ingestScreenBackendEvent(
      bot,
      {
        type: 'display.lease',
        connectionId: 'host-a',
        payload: { profile_key: '/other/profile', lease: { ...human, reason: 'other-bot' } }
      },
      null
    )
    expect(screenStateFor($screenState.get(), bot)?.lease).toBeUndefined()

    setScreenStatus(bot, stopped)
    flushScreenBackendEvents(bot, key)
    expect(screenStateFor($screenState.get(), bot)?.lease).toEqual(human)
  })
})

describe('useScreenBackendEvents', () => {
  it('an open surface picks up an external start via display.status', () => {
    setScreenStatus(bot, stopped)
    const view = renderHook(() => useScreenBackendEvents(bot))

    act(() =>
      emitGatewayEvent({
        type: 'display.status',
        connectionId: 'host-a',
        payload: running
      })
    )
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBe(true)
    view.unmount()
  })

  it('keeps a fresh pull when a stale status was buffered before the profile key', async () => {
    const request = vi.mocked(host.requestProfile)
    request.mockReset()
    request.mockResolvedValue(stopped)

    const view = renderHook(() => useScreenBackendEvents(bot))

    act(() =>
      emitGatewayEvent({
        type: 'display.status',
        connectionId: 'host-a',
        payload: running
      })
    )
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBeUndefined()

    await act(async () => {
      setScreenStatus(bot, stopped)
    })
    expect(screenStateFor($screenState.get(), bot)?.status?.running).toBe(false)
    expect(request).toHaveBeenCalledWith(expect.anything(), 'display.status', {})
    view.unmount()
  })

  it('replays a handoff that raced the first display.status', () => {
    const view = renderHook(() => useScreenBackendEvents(bot))

    act(() =>
      emitGatewayEvent({
        type: 'display.lease',
        connectionId: 'host-a',
        payload: { profile_key: key, lease: human }
      })
    )
    expect(screenStateFor($screenState.get(), bot)?.lease).toBeUndefined()

    act(() => setScreenStatus(bot, stopped))
    expect(screenStateFor($screenState.get(), bot)?.lease).toEqual(human)
    view.unmount()
  })
})
