import fs from 'node:fs'
import path from 'node:path'

export interface ResolveDesktopUpdateRootOptions {
  activeHermesRoot: string
  isHermesSourceRoot: (root: string) => boolean
  isPackaged: boolean
  overrideRoot?: string | null
  sourceRepoRoot: string
}

function canonicalRoot(root: string): string {
  const resolved = path.resolve(root)

  try {
    return fs.realpathSync.native ? fs.realpathSync.native(resolved) : fs.realpathSync(resolved)
  } catch {
    return resolved
  }
}

/**
 * Return the actual checkout behind a candidate install root.
 *
 * Managed Windows installs may expose the source checkout through a junction
 * at `%LOCALAPPDATA%\\hermes\\hermes-agent`. Resolve the junction before
 * looking for Git metadata so self-update operates on the real checkout and
 * does not depend on reparse-point traversal quirks in the packaged process.
 * `.git` may be either a directory (normal clone) or a file (Git worktree), so
 * existence — not `isDirectory()` — is the right contract.
 */
export function gitCheckoutRoot(root: string): string | null {
  const resolved = path.resolve(root)
  const canonical = canonicalRoot(resolved)
  const variants = canonical === resolved ? [resolved] : [canonical, resolved]

  for (const candidate of variants) {
    try {
      if (fs.existsSync(path.join(candidate, '.git'))) {
        return candidate
      }
    } catch {
      // Try the non-canonical spelling before treating this as non-Git.
    }
  }

  return null
}

function hermesSourceCandidate(root: string, isHermesSourceRoot: (candidate: string) => boolean): string | null {
  const resolved = path.resolve(root)
  const canonical = canonicalRoot(resolved)

  for (const candidate of canonical === resolved ? [resolved] : [canonical, resolved]) {
    if (isHermesSourceRoot(candidate)) {
      return resolved
    }
  }

  return null
}

/** Resolve the source tree the desktop's self-update operations should own. */
export function resolveDesktopUpdateRoot(options: ResolveDesktopUpdateRootOptions): string {
  const candidates = [
    options.overrideRoot && path.resolve(options.overrideRoot),
    !options.isPackaged && hermesSourceCandidate(options.sourceRepoRoot, options.isHermesSourceRoot),
    hermesSourceCandidate(options.activeHermesRoot, options.isHermesSourceRoot)
  ].filter((candidate): candidate is string => Boolean(candidate))

  for (const candidate of candidates) {
    const checkout = gitCheckoutRoot(candidate)

    if (checkout) {
      return checkout
    }
  }

  return candidates[0] || path.resolve(options.activeHermesRoot)
}
