import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { DisplayStatus } from './screen-connection'
import type * as ScreenConnection from './screen-connection'
import type { RosterRow } from './types'

const $testGateway = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof import('nanostores')

  return atom('open')
})

const sockets = vi.hoisted(
  () => [] as Array<{ closeCodes: number[]; closed: boolean; close: (code?: number) => void; serverClose: (code: number) => void }>
)

const rfbs = vi.hoisted(() => [] as Array<{ emit: (type: string, detail?: unknown) => void }>)
const retention = vi.hoisted(() => ({ held: 0 }))

vi.mock('@hermes/plugin-sdk', async () => {
  const { useStore } = await import('@nanostores/react')
  const { onGatewayEvent } = await import('../../contrib/events')

  return {
    Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
      <button {...props}>{children}</button>
    ),
    Codicon: () => null,
    GlyphSpinner: () => null,
    EmptyState: () => null,
    useValue: useStore,
    host: {
      onEvent: onGatewayEvent,
      retainProfile: async () => {
        retention.held += 1

        return () => {
          retention.held -= 1
        }
      },
      state: { gateway: $testGateway }
    }
  }
})
vi.mock('./routing', () => ({
  botConnectionRoute: () => ({ connectionId: 'host-a', profile: 'default', targetProfile: 'default' })
}))
vi.mock('./data', () => ({ botSelectionKey: (bot: RosterRow) => bot.name }))
vi.mock('./i18n', () => ({
  useBots: () => ({
    screen: {
      title: 'Screen',
      controlTaken: 'Another viewer took control',
      youControl: 'You control',
      otherControls: 'Other controls',
      handBack: 'Hand back',
      handBackForce: 'Hand back (force)',
      handBackForceHint: 'Force',
      takeOver: 'Take over',
      reconnect: 'Reconnect',
      streamLost: 'Stream lost',
      heroConnecting: 'Checking the screen…',
      stoppedTitle: 'Screen is off',
      stoppedBody: 'Start this bot’s desktop.',
      start: 'Start screen'
    }
  })
}))
vi.mock('./screen-connection', async importActual => ({
  // Real pure helpers (viewerHash / leaseHeldBy); only the gateway legs are faked.
  ...(await importActual<typeof ScreenConnection>()),
  displayRequest: vi.fn(),
  resolveScreenWsUrl: vi.fn(async () => 'ws://localhost/api/display/ws'),
  isEventForBotScreen: () => false
}))
vi.mock('@novnc/novnc', () => ({
  default: class {
    private listeners = new Map<string, Array<(event: { detail?: unknown }) => void>>()
    constructor(
      _target: HTMLElement,
      private socket: { close: () => void }
    ) {
      rfbs.push(this)
    }
    emit(type: string, detail?: unknown) {
      for (const listener of this.listeners.get(type) ?? []) {
        listener({ detail })
      }
    }
    addEventListener(type: string, callback: (event: { detail?: unknown }) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback])

      if (type === 'connect') {
        queueMicrotask(() => callback({}))
      }
    }
    // noVNC 1.7 disconnects its WebSocket without a close code.
    disconnect() {
      this.socket.close()
    }
    focus() {}
  }
}))

import { SCREEN_STATUS_RETRY_MS } from './screen-events'
import { displayRequest } from './screen-connection'
import { BotScreenPane } from './screen-pane'
import { $screenState, setScreenStatus } from './screen-state'

const bot: RosterRow = { name: 'default' }

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
  lease: { holder: 'human', viewer_id: 'this-viewer', pending_handoff: null, since: 1, reason: '' }
}

beforeEach(() => {
  $screenState.set({})
  $testGateway.set('open')
  sockets.length = 0
  rfbs.length = 0
  retention.held = 0
  vi.mocked(displayRequest)
    .mockReset()
    .mockResolvedValue({ ...status, ticket: 'test-ticket', viewer_id: 'this-viewer' })
  vi.stubGlobal(
    'WebSocket',
    class {
      closeCodes: number[] = []
      closed = false
      private onClose: Array<(event: { code: number }) => void> = []
      constructor() {
        sockets.push(this)
      }
      addEventListener(type: string, listener: (event: { code: number }) => void) {
        if (type === 'close') {
          this.onClose.push(listener)
        }
      }
      // The bridge closing us: the raw close frame reaches our listener, then noVNC
      // reports a statusless `disconnect` — the code is only on the socket event.
      serverClose(code: number) {
        this.closed = true

        for (const listener of this.onClose) {
          listener({ code })
        }
      }
      close(code?: number) {
        // Subsequent close calls cannot replace the frame already sent to the server.
        if (this.closed) {
          return
        }

        this.closed = true
        this.closeCodes.push(code ?? 1005)
      }
    }
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('sends an intentional close before noVNC can send its statusless close on pane unmount', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  view.unmount()
  expect(sockets[0].closeCodes).toEqual([1000])
})

it('pins the bot socket for the attach lifetime and lets go on unmount', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  expect(retention.held).toBe(1)
  fireEvent.click(view.getByTitle('Reconnect'))
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(retention.held).toBe(1)
  view.unmount()
  expect(retention.held).toBe(0)
})

it('does not hand back while replacing a stream to reconnect the same viewer', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  fireEvent.click(view.getByTitle('Reconnect'))
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(sockets[0].closeCodes).toEqual([1005])
  expect(sockets[1].closed).toBe(false)
  view.unmount()
})

it('asks observe to keep the minted viewer id so a same-connection reconnect does not remint', async () => {
  vi.mocked(displayRequest).mockImplementation(async (_bot, method, params = {}) => {
    if (method === 'display.observe') {
      const requested = typeof params.viewer_id === 'string' && params.viewer_id ? params.viewer_id : 'this-viewer'

      return { ...status, ticket: 't', viewer_id: requested }
    }

    return { ...status }
  })

  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  fireEvent.click(view.getByTitle('Reconnect'))
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(vi.mocked(displayRequest)).toHaveBeenCalledWith(bot, 'display.observe', { viewer_id: 'this-viewer' })
  expect(vi.mocked(displayRequest)).not.toHaveBeenCalledWith(bot, 'display.lease.acquire', expect.anything())
  view.unmount()
})

it('transfers the lease onto the reminted viewer id when reconnecting while holding', async () => {
  let observes = 0
  vi.mocked(displayRequest).mockImplementation(async (_bot, method, params = {}) => {
    if (method === 'display.observe') {
      observes += 1

      return { ...status, ticket: `t${observes}`, viewer_id: observes === 1 ? 'this-viewer' : 'this-viewer-2' }
    }

    if (method === 'display.lease.acquire') {
      return { lease: { ...status.lease, holder: 'human', viewer_id: String(params.viewer_id) } }
    }

    return { ...status }
  })

  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  fireEvent.click(view.getByTitle('Reconnect'))
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(vi.mocked(displayRequest)).toHaveBeenCalledWith(bot, 'display.observe', { viewer_id: 'this-viewer' })
  expect(vi.mocked(displayRequest)).toHaveBeenCalledWith(bot, 'display.lease.acquire', { viewer_id: 'this-viewer-2' })
  view.unmount()
})

it('does not steal the lease back if another viewer took over during reconnect', async () => {
  let observes = 0
  vi.mocked(displayRequest).mockImplementation(async (_bot, method) => {
    if (method === 'display.observe') {
      observes += 1

      return {
        ...status,
        ticket: `t${observes}`,
        viewer_id: observes === 1 ? 'this-viewer' : 'this-viewer-2',
        lease:
          observes === 1
            ? status.lease
            : { ...status.lease, holder: 'human', viewer_id: 'other-viewer' }
      }
    }

    return { ...status }
  })

  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  fireEvent.click(view.getByTitle('Reconnect'))
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(vi.mocked(displayRequest)).not.toHaveBeenCalledWith(bot, 'display.lease.acquire', expect.anything())
  view.unmount()
})

it('offers force hand-back on the stopped pane when a human lease survived the crash', async () => {
  vi.mocked(displayRequest).mockResolvedValue({
    ...status,
    running: false,
    pid: null,
    lease: { ...status.lease, holder: 'human', viewer_id: 'ghost-viewer' }
  })
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(view.getByText('Screen is off')).toBeTruthy())
  fireEvent.click(view.getByText('Hand back (force)'))
  await waitFor(() =>
    expect(vi.mocked(displayRequest)).toHaveBeenCalledWith(bot, 'display.lease.release', { force: true })
  )
  view.unmount()
})

it('retries display.status after a transient failure instead of showing empty live chrome', async () => {
  vi.useFakeTimers()
  let statusCalls = 0
  vi.mocked(displayRequest).mockImplementation(async (_bot, method) => {
    if (method === 'display.status') {
      statusCalls += 1

      if (statusCalls === 1) {
        throw new Error('502 Bad Gateway')
      }

      return { ...status }
    }

    return { ...status, ticket: 't', viewer_id: 'this-viewer' }
  })

  const view = render(<BotScreenPane bot={bot} />)
  await act(async () => {})
  expect(view.getByText('Checking the screen…')).toBeTruthy()
  expect(view.queryByTitle('Reconnect')).toBeNull()
  expect(sockets).toHaveLength(0)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(SCREEN_STATUS_RETRY_MS)
  })
  await act(async () => {})
  expect(sockets).toHaveLength(1)
  expect(view.queryByText('Checking the screen…')).toBeNull()
  view.unmount()
})

it('releases the pinned socket when display.observe fails after retain', async () => {
  vi.mocked(displayRequest).mockImplementation(async (_bot, method) => {
    if (method === 'display.observe') {
      throw new Error('observe failed')
    }

    return { ...status }
  })

  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(view.getByText('observe failed')).toBeTruthy())
  expect(retention.held).toBe(0)
  expect(sockets).toHaveLength(0)
  view.unmount()
})

it('releases the pinned socket when the RFB URL cannot be resolved after retain', async () => {
  const { resolveScreenWsUrl } = await import('./screen-connection')
  vi.mocked(resolveScreenWsUrl).mockRejectedValueOnce(new Error('no display ticket'))

  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(view.getByText('no display ticket')).toBeTruthy())
  expect(retention.held).toBe(0)
  expect(sockets).toHaveLength(0)
  view.unmount()
})

it('re-pulls display.status and re-attaches when the gateway reconnects while the pane is already open', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  const statusCalls = vi.mocked(displayRequest).mock.calls.filter(([, method]) => method === 'display.status').length

  await act(async () => {
    $testGateway.set('idle')
  })
  await act(async () => {
    $testGateway.set('open')
  })
  await waitFor(() => {
    const after = vi.mocked(displayRequest).mock.calls.filter(([, method]) => method === 'display.status').length
    expect(after).toBeGreaterThan(statusCalls)
  })
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(sockets[1].closed).toBe(false)
  view.unmount()
})

it('releases the RFB socket when the screen stops while the pane stays mounted', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})
  expect(retention.held).toBe(1)

  act(() => setScreenStatus(bot, { ...status, running: false, pid: null, display: null, socket: null }))
  await waitFor(() => expect(view.getByText('Screen is off')).toBeTruthy())
  expect(retention.held).toBe(0)
  expect(sockets[0].closed).toBe(true)
  expect(sockets[0].closeCodes).not.toContain(1000)
  view.unmount()
})

it('shows the control-taken overlay from the bridge close code, which noVNC does not forward', async () => {
  const view = render(<BotScreenPane bot={bot} />)
  await waitFor(() => expect(sockets).toHaveLength(1))
  await act(async () => {})

  act(() => {
    sockets[0].serverClose(4000)
    rfbs[0].emit('disconnect', { clean: true })
  })
  expect(view.getByText('Another viewer took control')).toBeTruthy()
  await waitFor(() => expect(sockets).toHaveLength(2))
  expect(sockets[1].closed).toBe(false)
  expect(vi.mocked(displayRequest)).not.toHaveBeenCalledWith(bot, 'display.lease.acquire', expect.anything())
  view.unmount()
})
