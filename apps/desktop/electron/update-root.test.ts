import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { gitCheckoutRoot, resolveDesktopUpdateRoot } from './update-root'

function makeHermesSource(root: string, gitKind: 'directory' | 'file' = 'directory'): void {
  fs.mkdirSync(path.join(root, 'hermes_cli'), { recursive: true })
  fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '', 'utf8')

  if (gitKind === 'file') {
    fs.writeFileSync(path.join(root, '.git'), 'gitdir: elsewhere\n', 'utf8')
  } else {
    fs.mkdirSync(path.join(root, '.git'))
  }
}

const isHermesSourceRoot = (root: string): boolean => fs.existsSync(path.join(root, 'hermes_cli', 'main.py'))

test('gitCheckoutRoot resolves a managed-install junction to its real checkout', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-root-'))
  const checkout = path.join(temp, 'source')
  const activeRoot = path.join(temp, 'hermes-agent')

  try {
    makeHermesSource(checkout)
    fs.symlinkSync(checkout, activeRoot, process.platform === 'win32' ? 'junction' : 'dir')

    assert.equal(gitCheckoutRoot(activeRoot), fs.realpathSync.native(checkout))
    assert.equal(
      resolveDesktopUpdateRoot({
        activeHermesRoot: activeRoot,
        isHermesSourceRoot,
        isPackaged: true,
        sourceRepoRoot: path.join(temp, 'packaged-app')
      }),
      fs.realpathSync.native(checkout)
    )
  } finally {
    fs.rmSync(temp, { force: true, recursive: true })
  }
})

test('gitCheckoutRoot accepts the .git file used by linked Git worktrees', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-root-'))

  try {
    makeHermesSource(temp, 'file')
    assert.equal(gitCheckoutRoot(temp), fs.realpathSync.native(temp))
  } finally {
    fs.rmSync(temp, { force: true, recursive: true })
  }
})

test('resolveDesktopUpdateRoot preserves priority and non-Git fallback behavior', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-root-'))
  const overrideRoot = path.join(temp, 'override')
  const activeRoot = path.join(temp, 'active')

  try {
    makeHermesSource(overrideRoot)
    makeHermesSource(activeRoot)

    assert.equal(
      resolveDesktopUpdateRoot({
        activeHermesRoot: activeRoot,
        isHermesSourceRoot,
        isPackaged: true,
        overrideRoot,
        sourceRepoRoot: path.join(temp, 'source')
      }),
      fs.realpathSync.native(overrideRoot)
    )

    fs.rmSync(path.join(overrideRoot, '.git'), { force: true, recursive: true })
    fs.rmSync(path.join(activeRoot, '.git'), { force: true, recursive: true })

    assert.equal(
      resolveDesktopUpdateRoot({
        activeHermesRoot: activeRoot,
        isHermesSourceRoot,
        isPackaged: true,
        overrideRoot,
        sourceRepoRoot: path.join(temp, 'source')
      }),
      path.resolve(overrideRoot)
    )
  } finally {
    fs.rmSync(temp, { force: true, recursive: true })
  }
})
