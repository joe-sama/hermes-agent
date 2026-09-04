type MainWindowLike = {
  isDestroyed: () => boolean
}

type EnsureMainWindowOptions<T extends MainWindowLike> = {
  isReady: boolean
  createWindow: () => unknown
  focusWindow: (window: T) => unknown
  focusExisting?: boolean
  quitInProgress?: boolean
  relaunchAfterQuit?: () => unknown
}

export function ensureMainWindow<T extends MainWindowLike>(
  window: T | null | undefined,
  {
    isReady,
    createWindow,
    focusWindow,
    focusExisting = true,
    quitInProgress = false,
    relaunchAfterQuit
  }: EnsureMainWindowOptions<T>
) {
  // A second launch can arrive after window-all-closed has started async
  // backend teardown but before the primary process releases its instance
  // lock. Never recreate a window in that dying process: the pending quit
  // would close it again and swallow the user's launch.
  if (quitInProgress) {
    relaunchAfterQuit?.()

    return
  }

  if (!window || window.isDestroyed()) {
    // a closed electron window stays truthy, so replace it before invoking native methods.
    if (isReady) {
      createWindow()
    }

    return
  }

  if (focusExisting) {
    focusWindow(window)
  }
}

type RelaunchAfterQuitRequest = {
  args: readonly string[]
  carriesDeepLink?: boolean
}

export const START_HIDDEN_FLAG = '--start-hidden'

/**
 * Decide whether each primary window created by this process should reveal.
 * The startup flag is deliberately one-shot: it suppresses only the cold-start
 * window, while a replacement window created after a crash still opens
 * normally. Re-opening an existing hidden window is handled by focusWindow().
 */
export function createMainWindowRevealPolicy(argv: readonly string[]) {
  let firstWindow = true
  const startHidden = argv.includes(START_HIDDEN_FLAG)

  return {
    takeShouldReveal() {
      const shouldReveal = !firstWindow || !startHidden

      firstWindow = false

      return shouldReveal
    }
  }
}

type MaximizedWindowTarget = {
  isDestroyed: () => boolean
  maximize: () => void
  once: (event: 'show', listener: () => void) => void
}

/**
 * Restore a saved maximized state without defeating a hidden cold start.
 * Electron's maximize() can make a hidden BrowserWindow visible on Windows,
 * so a background launch defers it until the user's first explicit show.
 */
export function restoreSavedMaximizedState(window: MaximizedWindowTarget, wasMaximized: boolean) {
  if (!wasMaximized) {
    return
  }

  const maximizeIfAlive = () => {
    if (!window.isDestroyed()) {
      window.maximize()
    }
  }

  window.once('show', maximizeIfAlive)
}

export function createRelaunchAfterQuitCoordinator() {
  let pending: { args: string[]; carriesDeepLink: boolean } | null = null

  return {
    queue(request: RelaunchAfterQuitRequest) {
      const next = {
        args: [...request.args],
        carriesDeepLink: request.carriesDeepLink === true
      }

      // A deep link carries more specific intent than a plain shortcut click.
      // Preserve it if another normal launch arrives before the process exits;
      // a newer deep link may replace an older one.
      if (!pending || next.carriesDeepLink || !pending.carriesDeepLink) {
        pending = next
      }
    },
    take({ handoff = false }: { handoff?: boolean } = {}) {
      const request = pending

      pending = null

      return handoff ? null : request
    }
  }
}

export function filterConsumedDeepLinkArgs(args: readonly string[], isDeepLink: (arg: string) => boolean): string[] {
  return args.filter(arg => !isDeepLink(arg))
}

export function shouldHideMainWindowOnClose({
  platform,
  quitTeardownStarted,
  quittingForHandoff
}: {
  platform: NodeJS.Platform
  quitTeardownStarted: boolean
  quittingForHandoff: boolean
}): boolean {
  // On Windows, the title-bar X is a background action. Explicit app.quit()
  // paths enter before-quit first and latch quitTeardownStarted, while updater
  // and uninstall handoffs latch quittingForHandoff before closing windows.
  // Keeping this decision pure prevents a future lifecycle edit from turning
  // X back into an accidental full shutdown.
  return platform === 'win32' && !quitTeardownStarted && !quittingForHandoff
}
