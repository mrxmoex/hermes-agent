/**
 * Two remote hosts can share the same `~/.hermes` path, so a `display.*` event
 * matched on `profile_key` alone would let host B's take-over repaint host A's
 * screen pane. The event must also have arrived on the bot's own connection.
 */

import { describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

const routeMock = vi.fn<() => { connectionId: string; profile: string } | null>(() => null)

vi.mock('@hermes/plugin-sdk', () => ({
  host: { requestProfile: vi.fn() },
  resolveSiblingWsUrl: vi.fn()
}))

vi.mock('./routing', () => ({
  botConnectionRoute: () => routeMock(),
  resolveBotConnectionRoute: () => {
    const route = routeMock()

    return route ? { route, status: 'resolved' } : { route: null, status: 'not_scoped' }
  }
}))

import { isEventForBotScreen } from './screen-connection'

const bot = { name: 'ops' } as RosterRow
const key = '/home/hermes/.hermes'

describe('isEventForBotScreen', () => {
  it('ignores a same-profile-path event that arrived from another host', () => {
    routeMock.mockReturnValue({ connectionId: 'conn-a', profile: 'ops' })

    const fromB = { connectionId: 'conn-b', payload: { profile_key: key }, type: 'display.lease' }
    const fromA = { connectionId: 'conn-a', payload: { profile_key: key }, type: 'display.lease' }

    expect(isEventForBotScreen(bot, fromB, key)).toBe(false)
    expect(isEventForBotScreen(bot, fromA, key)).toBe(true)
  })

  it('still matches the untagged local socket for a local bot', () => {
    routeMock.mockReturnValue({ connectionId: 'local', profile: 'ops' })

    expect(isEventForBotScreen(bot, { payload: { profile_key: key }, type: 'display.lease' }, key)).toBe(true)
    expect(isEventForBotScreen(bot, { payload: { profile_key: '/other' }, type: 'display.lease' }, key)).toBe(false)
  })

  it('matches by payload profile name when the home path is not known yet', () => {
    routeMock.mockReturnValue({ connectionId: 'conn-a', profile: 'ops' })

    const named = { connectionId: 'conn-a', payload: { profile: 'ops', profile_key: key }, type: 'display.lease' }
    const sibling = { connectionId: 'conn-a', payload: { profile: 'other', profile_key: '/other' }, type: 'display.lease' }
    const otherHost = { connectionId: 'conn-b', payload: { profile: 'ops', profile_key: key }, type: 'display.lease' }
    const unnamed = { connectionId: 'conn-a', payload: { profile_key: key }, type: 'display.lease' }
    const socketProfile = { connectionId: 'conn-a', profile: 'ops', payload: { profile_key: key }, type: 'display.lease' }

    expect(isEventForBotScreen(bot, named, undefined)).toBe(true)
    expect(isEventForBotScreen(bot, sibling, undefined)).toBe(false)
    expect(isEventForBotScreen(bot, otherHost, undefined)).toBe(false)
    expect(isEventForBotScreen(bot, unnamed, undefined)).toBe(false)
    expect(isEventForBotScreen(bot, socketProfile, undefined)).toBe(false)
  })

  it('matches an alias row by the backend target profile before the home path is known', () => {
    const event = { connectionId: 'conn-a', payload: { profile: 'default', profile_key: key }, type: 'display.lease' }

    routeMock.mockReturnValue({ connectionId: 'conn-a', profile: 'moxie', targetProfile: 'default' })
    expect(isEventForBotScreen({ name: 'moxie', targetProfile: 'default' } as RosterRow, event, undefined)).toBe(true)

    routeMock.mockReturnValue({ connectionId: 'conn-a', profile: 'ops', targetProfile: 'ops' })
    expect(isEventForBotScreen(bot, event, undefined)).toBe(false)
  })
})
