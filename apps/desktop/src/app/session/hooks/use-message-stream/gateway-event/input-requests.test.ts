import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $secretRequest, $sudoRequest, clearAllPrompts, setSecretRequest, setSudoRequest } from '@/store/prompts'
import { $activeSessionId } from '@/store/session'

import { handleInputRequestEvent } from './input-requests'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))

function promptEvent(type: string, payload: Record<string, unknown>, sessionId = 's1'): GatewayEventContext {
  return {
    deps: {
      activeSessionIdRef: { current: 's1' },
      sessionInterrupted: () => false,
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    },
    event: { session_id: sessionId, type },
    explicitSid: sessionId,
    fromActiveSource: () => true,
    isActiveEvent: sessionId === 's1',
    occurredAt: Date.now() / 1000,
    payload,
    scheduleConfigRefresh: vi.fn(),
    sessionId
  } as unknown as GatewayEventContext
}

beforeEach(() => {
  $activeSessionId.set('s1')
})

afterEach(() => {
  clearAllPrompts()
  $activeSessionId.set(null)
  vi.clearAllMocks()
})

describe('handleInputRequestEvent sensitive prompts', () => {
  it('keeps only whitelisted transient target metadata', () => {
    const sentinel = 'raw-secret-must-not-enter-the-store'

    handleInputRequestEvent(
      promptEvent('secret.request', {
        env_var: 'Account password',
        metadata: {
          kind: 'computer_use',
          secret: sentinel,
          target: {
            app_name: 'Browser',
            pid: '123',
            secret: sentinel,
            title: 'Sign in',
            window_id: 456
          },
          text: sentinel,
          transient: true,
          value: sentinel
        },
        prompt: 'Enter the password to type into the selected window.',
        request_id: 'secret-1'
      })
    )

    expect($secretRequest.get()).toEqual({
      envVar: 'Account password',
      metadata: {
        kind: 'computer_use',
        target: { appName: 'Browser', pid: 123, title: 'Sign in', windowId: 456 },
        transient: true
      },
      prompt: 'Enter the password to type into the selected window.',
      requestId: 'secret-1',
      sessionId: 's1'
    })
    expect(JSON.stringify($secretRequest.get())).not.toContain(sentinel)
  })

  it('expires only the matching secret request in the matching session', () => {
    setSecretRequest({ envVar: 'Password', prompt: 'Enter it', requestId: 'secret-new', sessionId: 's1' })

    handleInputRequestEvent(promptEvent('secret.expire', { request_id: 'secret-new' }, 's2'))
    handleInputRequestEvent(promptEvent('secret.expire', { request_id: 'secret-old' }))
    expect($secretRequest.get()?.requestId).toBe('secret-new')

    handleInputRequestEvent(promptEvent('secret.expire', { request_id: 'secret-new' }))
    expect($secretRequest.get()).toBeNull()
  })

  it('expires only the matching sudo request', () => {
    setSudoRequest({ requestId: 'sudo-new', sessionId: 's1' })

    handleInputRequestEvent(promptEvent('sudo.expire', { request_id: 'sudo-old' }))
    expect($sudoRequest.get()?.requestId).toBe('sudo-new')

    handleInputRequestEvent(promptEvent('sudo.expire', { request_id: 'sudo-new' }))
    expect($sudoRequest.get()).toBeNull()
  })
})
