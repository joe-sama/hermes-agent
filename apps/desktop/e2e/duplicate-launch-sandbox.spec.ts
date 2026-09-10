import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { _electron, expect, test } from '@playwright/test'

import { buildAppEnv, findElectron } from './fixtures'

test('duplicate launches never change the primary Windows sandbox marker', async () => {
  test.skip(process.platform !== 'win32', 'Windows sandbox lifecycle contract')
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-duplicate-launch-'))
  const hermesHome = path.join(root, 'hermes-home')
  const userDataDir = path.join(root, 'electron-user-data')
  fs.mkdirSync(hermesHome)
  fs.mkdirSync(userDataDir)
  fs.writeFileSync(path.join(hermesHome, 'config.yaml'), 'local_runtime:\n  enabled: false\n')
  const cleanup = () => fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
  const env = buildAppEnv({ root, hermesHome, userDataDir, cleanup }, { HERMES_DESKTOP_APP_NAME: 'HermesSandboxRegression' })
  const desktopRoot = path.resolve(import.meta.dirname, '..')
  const executablePath = findElectron()
  const markerPath = path.join(userDataDir, 'windows-sandbox-fallback.json')
  const app = await _electron.launch({
    executablePath, args: [desktopRoot, '--start-hidden'], env, cwd: desktopRoot, timeout: 45_000
  })
  try {
    await expect.poll(() => fs.existsSync(markerPath)).toBe(true)
    // A handoff during primary boot must not count as a crash either. Use an
    // isolated marker fixture and verify byte-for-byte immutability by children.
    const marker = JSON.stringify({ state: 'booting', bootAborts: 1 }) + '\n'
    await app.evaluate(async ({ app }) => { await app.whenReady() })
    const page = await app.firstWindow()
    await page.waitForLoadState('domcontentloaded')
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some(window => window.isVisible()))).toBe(false)
    for (let attempt = 0; attempt < 3; attempt++) {
      fs.writeFileSync(markerPath, marker)
      const child = spawn(executablePath, [desktopRoot, '--start-hidden'], {
        env, cwd: desktopRoot, windowsHide: true, stdio: 'ignore'
      })
      const code = await new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => { child.kill(); reject(new Error('Duplicate did not exit')) }, 15_000)
        child.once('error', error => { clearTimeout(timer); reject(error) })
        child.once('exit', value => { clearTimeout(timer); resolve(value) })
      })
      expect(code).toBe(0)
      expect(fs.readFileSync(markerPath, 'utf8')).toBe(marker)
      expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some(window => window.isVisible()))).toBe(false)
    }
    expect(await app.evaluate(({ app }) => app.commandLine.hasSwitch('no-sandbox'))).toBe(false)
  } finally {
    await app.close()
    cleanup()
  }
})
