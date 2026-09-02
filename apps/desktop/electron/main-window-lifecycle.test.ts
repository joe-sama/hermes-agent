import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createRelaunchAfterQuitCoordinator,
  ensureMainWindow,
  filterConsumedDeepLinkArgs,
  shouldHideMainWindowOnClose
} from './main-window-lifecycle'

test('recreates a destroyed primary window without focusing it', () => {
  const destroyedWindow = {
    isDestroyed: () => true
  }

  let createCalls = 0
  let focusCalls = 0

  ensureMainWindow(destroyedWindow, {
    isReady: true,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => {
      focusCalls += 1
    }
  })

  assert.equal(createCalls, 1)
  assert.equal(focusCalls, 0)
})

test('waits for app readiness before recreating a primary window', () => {
  let createCalls = 0

  ensureMainWindow(null, {
    isReady: false,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => assert.fail('missing window must not be focused')
  })

  assert.equal(createCalls, 0)
})

test('focuses a live primary window for a normal second launch', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  let focusedWindow = null

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: window => {
      focusedWindow = window
    }
  })

  assert.equal(focusedWindow, liveWindow)
})

test('leaves live-window focus to deep-link delivery', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: () => assert.fail('deep-link delivery owns focus'),
    focusExisting: false
  })
})

test('queues a relaunch instead of restoring a window while quit teardown is running', () => {
  let relaunchCalls = 0

  ensureMainWindow(null, {
    isReady: true,
    createWindow: () => assert.fail('a quitting process must not create a replacement window'),
    focusWindow: () => assert.fail('a quitting process must not focus a window'),
    quitInProgress: true,
    relaunchAfterQuit: () => {
      relaunchCalls += 1
    }
  })

  assert.equal(relaunchCalls, 1)
})

test('coalesces quit-time launches and preserves the most specific deep-link intent', () => {
  const coordinator = createRelaunchAfterQuitCoordinator()

  coordinator.queue({ args: [] })
  coordinator.queue({ args: ['--profile=default'] })
  coordinator.queue({ args: ['hermes://blueprint/example'], carriesDeepLink: true })
  coordinator.queue({ args: [] })

  assert.deepEqual(coordinator.take(), {
    args: ['hermes://blueprint/example'],
    carriesDeepLink: true
  })
  assert.equal(coordinator.take(), null)
})

test('suppresses a queued normal relaunch when an updater handoff wins the final quit', () => {
  const coordinator = createRelaunchAfterQuitCoordinator()

  coordinator.queue({ args: [] })

  assert.equal(coordinator.take({ handoff: true }), null)
  assert.equal(coordinator.take(), null)
})

test('does not replay a deep link that the closing process already consumed', () => {
  assert.deepEqual(
    filterConsumedDeepLinkArgs(
      ['--profile=default', 'hermes://blueprint/already-opened', '--no-sandbox'],
      arg => arg.startsWith('hermes://')
    ),
    ['--profile=default', '--no-sandbox']
  )
})

test('keeps a normal Windows title-bar close running in the background', () => {
  assert.equal(
    shouldHideMainWindowOnClose({
      platform: 'win32',
      quitTeardownStarted: false,
      quittingForHandoff: false
    }),
    true
  )
})

test('allows explicit Windows quits and handoffs to close the main window', () => {
  assert.equal(
    shouldHideMainWindowOnClose({
      platform: 'win32',
      quitTeardownStarted: true,
      quittingForHandoff: false
    }),
    false
  )
  assert.equal(
    shouldHideMainWindowOnClose({
      platform: 'win32',
      quitTeardownStarted: false,
      quittingForHandoff: true
    }),
    false
  )
})

test('preserves the native close behavior on non-Windows platforms', () => {
  assert.equal(
    shouldHideMainWindowOnClose({
      platform: 'darwin',
      quitTeardownStarted: false,
      quittingForHandoff: false
    }),
    false
  )
  assert.equal(
    shouldHideMainWindowOnClose({
      platform: 'linux',
      quitTeardownStarted: false,
      quittingForHandoff: false
    }),
    false
  )
})
