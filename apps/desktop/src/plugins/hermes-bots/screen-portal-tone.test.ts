import { describe, expect, it } from 'vitest'

import type { DisplayLease, DisplayStatus, ScreenViewer } from './screen-connection'
import { portalTone } from './screen-portal'

const status: DisplayStatus = {
  profile: 'default',
  profile_key: '/home/hermes/.hermes',
  supported: true,
  installed: true,
  missing: [],
  running: true,
  pid: 42,
  display: ':20',
  socket: '/tmp/rfb.sock',
  geometry: '1440x900',
  install_command: null,
  lease: { holder: 'agent', viewer_id: null, viewer_hash: null, pending_handoff: null, since: 1, reason: '' }
}

const agent: DisplayLease = { holder: 'agent', viewer_id: null, viewer_hash: null, pending_handoff: null, since: 1, reason: '' }
const viewer: ScreenViewer = { id: 'v1', hash: 'abc' }

describe('portalTone', () => {
  it('surfaces a pending handoff on the hero and portal, not only the full pane', () => {
    expect(portalTone(status, { ...agent, pending_handoff: 'Sign in to GitHub' }, viewer)).toBe('handoff')
  })

  it('still prefers a live human holder over a leftover pending_handoff string', () => {
    const human: DisplayLease = { holder: 'human', viewer_id: 'v1', viewer_hash: 'abc', pending_handoff: null, since: 1, reason: 'Sign in' }
    expect(portalTone(status, human, viewer)).toBe('human')
  })

  it('stays live when the agent holds and nobody asked for help', () => {
    expect(portalTone(status, agent, viewer)).toBe('live')
  })
})
