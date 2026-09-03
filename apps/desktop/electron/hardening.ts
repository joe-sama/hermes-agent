import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Relative, not `@hermes/shared`: the electron bundle is built by esbuild with
// no tsconfig path resolution (see scripts/bundle-electron-main.mjs), so a bare
// specifier would typecheck and then fail to bundle.
import {
  clampDataUrlReadMaxMb,
  DATA_URL_READ_DEFAULT_MAX_MB,
  DATA_URL_READ_MAX_MAX_MB,
  DATA_URL_READ_MIN_MAX_MB
} from '../../shared/src/data-url-read-max'

const DEFAULT_FETCH_TIMEOUT_MS = 30_000
// Remote file.attach sends one base64 JSON-RPC frame. Cap the dedicated attach
// reader so the payload still fits uvicorn's raised ws_max_size (384 MiB)
// after base64 + framing. Preview stays on the Settings-configurable path.
const ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
const TEXT_PREVIEW_SOURCE_MAX_BYTES = 64 * 1024 * 1024

function dataUrlReadMaxBytesFromMb(maxMb) {
  return clampDataUrlReadMaxMb(maxMb) * 1024 * 1024
}

const SAFE_ENV_SUFFIXES = new Set(['dist', 'example', 'sample', 'template'])
const SENSITIVE_EXTENSIONS = new Set(['.kdbx', '.p12', '.pem', '.pfx'])

// Owner-only mode for userData files that carry credentials (the encrypted
// gateway token in connection.json, and the URL/SSH fields alongside it).
// connection.json was the odd one out: its two credential-bearing neighbours
// under userData are already 0600 — desktop-installation.json
// (desktop-installation.ts) and native-oauth-tokens.json (main.ts
// `_nativeTokenStoreIo`) — while connection.json was written with no mode at
// all and landed at the 0644 umask default. This makes the three consistent.
const SECRET_FILE_MODE = 0o600

// The encoding tag that marks a payload as OS-encrypted. One constant because
// the writer (encryptDesktopSecret, here) and the reader
// (decryptDesktopSecret, in main.ts) have to agree on the exact string across
// a file boundary, and the native-token store round-trips the same shape.
const SAFE_STORAGE_ENCODING = 'safeStorage'

interface SecretFileFs {
  chmodSync: typeof fs.chmodSync
  lstatSync: typeof fs.lstatSync
  renameSync: typeof fs.renameSync
  rmSync: typeof fs.rmSync
  writeFileSync: typeof fs.writeFileSync
}

interface WindowsOwnerOnlyAclCommand {
  executable: string
  args: string[]
  env: Record<string, string>
}

interface SecretFileOptions {
  encoding?: BufferEncoding
  fs?: SecretFileFs
  platform?: string
  windowsAclEnvironment?: NodeJS.ProcessEnv
  windowsAclRunner?: (command: WindowsOwnerOnlyAclCommand) => void
}

const WINDOWS_SECRET_ACL_PATH_ENV = 'HERMES_DESKTOP_SECRET_ACL_PATH'

/**
 * Build the shell-free Windows ACL command used for credential files.
 *
 * The file path travels in a minimal child environment instead of being
 * interpolated into PowerShell source. Besides avoiding quoting/injection
 * hazards, this keeps the command line free of userData path contents. The
 * script uses .NET directly, so it does not depend on localized icacls output.
 */
function buildWindowsOwnerOnlyAclCommand(
  filePath: string,
  options: { environment?: NodeJS.ProcessEnv } = {}
): WindowsOwnerOnlyAclCommand {
  const environment = options.environment || process.env

  const systemRoot = String(environment.SystemRoot || environment.SYSTEMROOT || environment.WINDIR || 'C:\\Windows')

  const script = String.raw`
$ErrorActionPreference = 'Stop'
$credentialPath = [Environment]::GetEnvironmentVariable('${WINDOWS_SECRET_ACL_PATH_ENV}', 'Process')
if ([String]::IsNullOrWhiteSpace($credentialPath) -or -not [IO.File]::Exists($credentialPath)) {
  throw 'Credential file is missing.'
}
$attributes = [IO.File]::GetAttributes($credentialPath)
if (($attributes -band [IO.FileAttributes]::Directory) -ne 0 -or
    ($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw 'Credential path is not a regular file.'
}
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$before = [IO.File]::GetAccessControl(
  $credentialPath,
  [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner
)
$ownerSid = $before.GetOwner([Security.Principal.SecurityIdentifier])
if ($ownerSid.Value -ne $currentSid.Value) {
  throw 'Credential file is not owned by the current user.'
}
$next = New-Object Security.AccessControl.FileSecurity
$next.SetOwner($currentSid)
$next.SetAccessRuleProtection($true, $false)
$rule = New-Object Security.AccessControl.FileSystemAccessRule(
  $currentSid,
  [Security.AccessControl.FileSystemRights]::FullControl,
  [Security.AccessControl.AccessControlType]::Allow
)
$next.AddAccessRule($rule)
[IO.File]::SetAccessControl($credentialPath, $next)
$verified = [IO.File]::GetAccessControl(
  $credentialPath,
  [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner
)
$verifiedOwner = $verified.GetOwner([Security.Principal.SecurityIdentifier])
$rules = @($verified.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
if ($verifiedOwner.Value -ne $currentSid.Value -or -not $verified.AreAccessRulesProtected -or $rules.Count -ne 1) {
  throw 'Credential ACL verification failed.'
}
$verifiedRule = $rules[0]
if ($verifiedRule.IsInherited -or
    $verifiedRule.IdentityReference.Value -ne $currentSid.Value -or
    $verifiedRule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
    ($verifiedRule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
      [Security.AccessControl.FileSystemRights]::FullControl) {
  throw 'Credential ACL verification failed.'
}
`

  return {
    executable: path.win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    args: ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
    env: {
      SystemRoot: systemRoot,
      WINDIR: systemRoot,
      [WINDOWS_SECRET_ACL_PATH_ENV]: filePath
    }
  }
}

function runWindowsOwnerOnlyAcl(filePath: string, options: SecretFileOptions): boolean {
  try {
    const command = buildWindowsOwnerOnlyAclCommand(filePath, {
      environment: options.windowsAclEnvironment
    })

    const runner =
      options.windowsAclRunner ||
      ((next: WindowsOwnerOnlyAclCommand) => {
        execFileSync(next.executable, next.args, {
          encoding: 'utf8',
          env: next.env,
          stdio: 'pipe',
          timeout: 15_000,
          windowsHide: true
        })
      })

    runner(command)

    return true
  } catch {
    return false
  }
}

/**
 * Tighten an existing credential file to owner-only (0600), returning whether
 * the file now has that mode.
 *
 * Exists because `fs.writeFileSync(path, data, { mode })` only applies `mode`
 * when it CREATES the file — rewriting an existing path silently keeps the old
 * bits. So a file already on disk at 0644 (every connection.json written
 * before this change, since that write passed no mode at all) needs an explicit
 * chmod; a fresh `mode:` alone would never tighten it.
 *
 * Guards match `readInstallationId` in desktop-installation.ts, which does the
 * same job for the sibling userData credential file: only ever chmod a regular
 * file we own, never a symlink and never another user's file. Without them a
 * symlink planted at the path would send the chmod to whatever it resolves to.
 *
 * Windows uses an explicit, protected DACL containing one full-control ACE for
 * the current owner. The ACL is read back and verified before success is
 * reported; chmod is never used there because Node maps it to the read-only
 * bit rather than access control.
 *
 * Never throws. Callers that are about to expose credential bytes decide
 * whether a false result is fatal; the atomic writer always treats it as fatal
 * and removes its temp file before any rename.
 */
function tightenSecretFileMode(filePath, options: SecretFileOptions = {}) {
  const fsImpl = options.fs || fs
  const platform = options.platform || process.platform

  try {
    const stat = fsImpl.lstatSync(filePath)

    if (!stat.isFile() || stat.isSymbolicLink()) {
      return false
    }

    if (platform === 'win32') {
      return runWindowsOwnerOnlyAcl(filePath, options)
    }

    if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
      return false
    }

    if ((stat.mode & 0o777) === SECRET_FILE_MODE) {
      return true
    }

    fsImpl.chmodSync(filePath, SECRET_FILE_MODE)

    return true
  } catch {
    return false
  }
}

function requireSecretFileProtection(filePath, options: SecretFileOptions = {}) {
  if (!tightenSecretFileMode(filePath, options)) {
    throw new Error('Could not establish owner-only access for the credential file.')
  }
}

/**
 * Atomically write a credential file owner-only. POSIX uses mode 0600;
 * Windows replaces inherited access with a verified owner-only DACL.
 *
 * The temp-then-rename dance is what makes the mode subtle: `renameSync` keeps
 * the TEMP file's permissions, so writing the temp at the default umask (0644)
 * hands those bits to the target — and a crashed earlier write can leave a
 * stale temp file whose existing loose bits `mode:` will not correct. Hence the
 * unlink of any stale temp (which also drops a planted symlink, so the write
 * cannot be redirected), the create-time `mode`, and the chmod before the
 * rename.
 */
function writeSecretFileAtomic(targetPath, data, options: SecretFileOptions = {}) {
  const fsImpl = options.fs || fs
  const tmp = targetPath + '.tmp'

  fsImpl.rmSync(tmp, { force: true })

  try {
    fsImpl.writeFileSync(tmp, data, { encoding: options.encoding, mode: SECRET_FILE_MODE })

    requireSecretFileProtection(tmp, options)

    fsImpl.renameSync(tmp, targetPath)
  } catch (error) {
    try {
      fsImpl.rmSync(tmp, { force: true })
    } catch {
      // Preserve the original failure; cleanup remains best effort.
    }

    throw error
  }
}

function resolveTimeoutMs(timeoutMs, fallbackMs = DEFAULT_FETCH_TIMEOUT_MS) {
  const fallback =
    Number.isFinite(fallbackMs) && Number(fallbackMs) > 0 ? Math.round(Number(fallbackMs)) : DEFAULT_FETCH_TIMEOUT_MS

  const parsed = Number(timeoutMs)

  if (Number.isFinite(parsed) && parsed > 0) {
    return Math.round(parsed)
  }

  return fallback
}

function encryptDesktopSecret(value, safeStorageApi, options: { allowPlainText?: boolean } = {}) {
  const raw = String(value || '')

  if (!raw) {
    return null
  }

  // Opt-in escape hatch for keyring-less Linux (e.g. Hyprland/Sway with no
  // GNOME Keyring or KWallet): the renderer sets this once the user confirms
  // the plain-text storage prompt in Settings → Gateway.
  const allowPlainText = options?.allowPlainText === true

  let encryptionAvailable = false

  try {
    encryptionAvailable = Boolean(safeStorageApi?.isEncryptionAvailable?.())
  } catch {
    encryptionAvailable = false
  }

  if (!encryptionAvailable) {
    // Only downgrade to plain text when the user has explicitly opted in;
    // decryptDesktopSecret returns the raw value for any non-'safeStorage'
    // encoding, so this round-trips without any decrypt-side change.
    if (allowPlainText) {
      return { encoding: 'plain', value: raw }
    }

    throw new Error(
      'Secure token storage is unavailable (no OS keyring service was found), so Hermes Desktop cannot save remote gateway tokens. ' +
        'Either enable an OS keyring (e.g. GNOME Keyring or KWallet providing org.freedesktop.secrets) and try again, ' +
        'confirm the plain-text storage option when prompted in Settings → Gateway, ' +
        'or set HERMES_DESKTOP_REMOTE_URL and HERMES_DESKTOP_REMOTE_TOKEN in your environment.'
    )
  }

  try {
    return {
      encoding: SAFE_STORAGE_ENCODING,
      value: safeStorageApi.encryptString(raw).toString('base64')
    }
  } catch (error) {
    const detail = error instanceof Error && error.message ? ` (${error.message})` : ''
    throw new Error(
      `Failed to encrypt the remote gateway token for secure storage${detail}. ` +
        'Set HERMES_DESKTOP_REMOTE_URL and HERMES_DESKTOP_REMOTE_TOKEN in your environment as a fallback.'
    )
  }
}

// Keyring-less Linux (e.g. Hyprland/Sway with no GNOME Keyring or KWallet):
// `--password-store=basic` selects Electron's built-in "basic" backend, but
// Electron only counts it as available once setUsePlainTextEncryption(true) is
// called. The caller runs this on whenReady, before createWindow() and anything
// that could touch safeStorage, so the switch takes effect for the whole run.
//
// Semantics are deliberately narrow: only linux, only the exact 'basic' switch
// value (never 'gnome-libsecret', 'kwallet', '', etc.), and only when the
// method exists (older/mocked safeStorage may lack it) and does not throw.
// Anything else is a no-op. Returns true only when it actually flipped the flag,
// so the caller (and tests) can distinguish "enabled" from "left untouched".
// Never throws: a failure here is non-fatal — encryption simply stays
// unavailable and the user can fall back to the plain-text opt-in or the
// HERMES_DESKTOP_REMOTE_* env vars.
function enableBasicPasswordStoreEncryption({ platform, passwordStoreSwitch, safeStorageApi }: any = {}) {
  if (platform !== 'linux' || passwordStoreSwitch !== 'basic') {
    return false
  }

  try {
    if (typeof safeStorageApi?.setUsePlainTextEncryption === 'function') {
      safeStorageApi.setUsePlainTextEncryption(true)

      return true
    }
  } catch {
    // Non-fatal: fall through and report that encryption was not enabled.
  }

  return false
}

// The token-persistence seam shared by the connection-config save/apply IPC
// path. Given the incoming edit, decide what token block to persist:
//   - No incoming token: keep the existing block's token untouched (edits that
//     don't retype the token must not clear it).
//   - persistToken false (the transient test-connection path): store the raw
//     value as a plain block WITHOUT touching secure storage — it is never
//     written to disk, so there is nothing to protect.
//   - Otherwise: run the incoming token through the injected encryptSecret
//     (encryptDesktopSecret in production), forwarding the plain-text opt-in.
//
// The plain-text opt-in is coerced with `=== true` HERE so the strictness lives
// in one place: a truthy-but-not-true value (1, 'yes', etc.) must NOT silently
// enable plain-text storage. Callers pass `allowPlainText` through raw.
function resolvePersistedRemoteToken({
  incomingToken,
  persistToken,
  existingToken,
  allowPlainText,
  encryptSecret
}: any = {}) {
  if (!incomingToken) {
    return existingToken
  }

  if (!persistToken) {
    return { encoding: 'plain', value: incomingToken }
  }

  return encryptSecret(incomingToken, { allowPlainText: allowPlainText === true })
}

function sensitiveFileBlockReason(filePath) {
  const normalized = String(filePath || '')
    .replace(/\\/g, '/')
    .toLowerCase()

  const basename = path.basename(normalized)
  const ext = path.extname(basename)

  if (!basename) {
    return null
  }

  if (normalized.includes('/.ssh/')) {
    return 'SSH key/config files are blocked.'
  }

  if (normalized.includes('/.gnupg/')) {
    return 'GPG key material is blocked.'
  }

  if (normalized.endsWith('/.aws/credentials')) {
    return 'AWS credential files are blocked.'
  }

  if (basename === '.env') {
    return '.env files are blocked because they commonly contain secrets.'
  }

  if (basename.startsWith('.env.')) {
    const suffix = basename.slice('.env.'.length)

    if (!SAFE_ENV_SUFFIXES.has(suffix)) {
      return `${basename} is blocked because it appears to contain environment secrets.`
    }
  }

  if (/^id_(rsa|dsa|ecdsa|ed25519)(?:\..+)?$/.test(basename) && !basename.endsWith('.pub')) {
    return 'SSH private key files are blocked.'
  }

  if (SENSITIVE_EXTENSIONS.has(ext)) {
    return `${ext} key/certificate files are blocked.`
  }

  if (basename === '.npmrc' || basename === '.netrc' || basename === '.pypirc') {
    return `${basename} is blocked because it may include auth credentials.`
  }

  return null
}

function ipcPathError(code: any, message: string): Error & { code: any } {
  const error = new Error(message) as Error & { code: any }

  ;(error as any).code = code

  return error
}

function rejectUnsafePathSyntax(filePath, purpose = 'File read') {
  if (typeof filePath !== 'string') {
    throw ipcPathError('invalid-path', `${purpose} failed: file path is required.`)
  }

  const raw = filePath.trim()

  if (!raw) {
    throw ipcPathError('invalid-path', `${purpose} failed: file path is required.`)
  }

  if (raw.includes('\0')) {
    throw ipcPathError('invalid-path', `${purpose} failed: file path is invalid.`)
  }

  const normalized = raw.replace(/\\/g, '/').toLowerCase()

  if (
    normalized.startsWith('//?/') ||
    normalized.startsWith('//./') ||
    normalized.startsWith('globalroot/device/') ||
    normalized.includes('/globalroot/device/')
  ) {
    throw ipcPathError('device-path', `${purpose} blocked: Windows device paths are not allowed.`)
  }

  return raw
}

function resolveRequestedPathForIpc(filePath, options: { purpose?: string; baseDir?: fs.PathOrFileDescriptor } = {}) {
  const purpose = String(options.purpose || 'File read')
  let raw = rejectUnsafePathSyntax(filePath, purpose)

  // Gateway-reported cwds (config `terminal.cwd`, remote sessions) routinely
  // arrive as `~/...`. Node's fs has no shell — without expansion the path
  // resolves under process.cwd() and every read "ENOENT"s forever.
  if (raw === '~' || raw.startsWith('~/') || raw.startsWith('~\\')) {
    raw = path.join(os.homedir(), raw.slice(1))
  }

  if (/^file:/i.test(raw)) {
    let resolvedPath

    try {
      const parsed = new URL(raw)

      if (parsed.protocol !== 'file:') {
        throw new Error('not a file URL')
      }

      resolvedPath = fileURLToPath(parsed)
    } catch {
      throw ipcPathError('invalid-path', `${purpose} failed: file URL is invalid.`)
    }

    rejectUnsafePathSyntax(resolvedPath, purpose)

    return path.resolve(resolvedPath)
  }

  const baseInput = typeof options.baseDir === 'string' && options.baseDir.trim() ? options.baseDir : process.cwd()
  const safeBaseInput = rejectUnsafePathSyntax(baseInput, purpose)
  const resolvedBase = path.resolve(safeBaseInput)
  rejectUnsafePathSyntax(resolvedBase, purpose)
  const resolvedPath = path.resolve(resolvedBase, raw)
  rejectUnsafePathSyntax(resolvedPath, purpose)

  return resolvedPath
}

async function statForIpc(fsImpl: { promises: { stat: typeof fs.promises.stat } }, resolvedPath, purpose, typeLabel) {
  try {
    return await fsImpl.promises.stat(resolvedPath)
  } catch (error) {
    const code = error && typeof error === 'object' ? error.code : ''

    if (code === 'ENOENT' || code === 'ENOTDIR') {
      throw ipcPathError(code || 'ENOENT', `${purpose} failed: ${typeLabel} does not exist.`)
    }

    throw ipcPathError(
      code || 'read-error',
      `${purpose} failed: ${error instanceof Error ? error.message : String(error)}`
    )
  }
}

async function realpathForIpc(fsImpl, resolvedPath, purpose) {
  if (typeof fsImpl.promises.realpath !== 'function') {
    return resolvedPath
  }

  try {
    const realPath = await fsImpl.promises.realpath(resolvedPath)
    rejectUnsafePathSyntax(realPath, purpose)

    return realPath
  } catch (error) {
    const code = error && typeof error === 'object' ? error.code : ''
    throw ipcPathError(
      code || 'read-error',
      `${purpose} failed: ${error instanceof Error ? error.message : String(error)}`
    )
  }
}

function rejectSensitiveFilePath(filePath, purpose) {
  const blockReason = sensitiveFileBlockReason(filePath)

  if (blockReason) {
    throw ipcPathError('sensitive-file', `${purpose} blocked for sensitive file: ${blockReason}`)
  }
}

async function resolveDirectoryForIpc(
  dirPath,
  options: {
    purpose?: string
    baseDir?: fs.PathOrFileDescriptor
    fs?: { promises: { stat: typeof fs.promises.stat } }
  } = {}
) {
  const purpose = String(options.purpose || 'Directory read')
  const fsImpl = options.fs || fs
  const resolvedPath = resolveRequestedPathForIpc(dirPath, { baseDir: options.baseDir, purpose })
  const stat = await statForIpc(fsImpl, resolvedPath, purpose, 'directory')

  if (!stat.isDirectory()) {
    throw ipcPathError('ENOTDIR', `${purpose} failed: path is not a directory.`)
  }

  const realPath = await realpathForIpc(fsImpl, resolvedPath, purpose)

  return { realPath, resolvedPath, stat }
}

async function resolveReadableFileForIpc(
  filePath,
  options: {
    purpose?: string
    baseDir?: fs.PathOrFileDescriptor
    fs?: typeof fs
    blockSensitive?: boolean
    maxBytes?: number
  } = {}
) {
  const purpose = String(options.purpose || 'File read')
  const fsImpl = options.fs || fs
  const resolvedPath = resolveRequestedPathForIpc(filePath, { baseDir: options.baseDir, purpose })

  if (options.blockSensitive !== false) {
    rejectSensitiveFilePath(resolvedPath, purpose)
  }

  const stat = await statForIpc(fsImpl, resolvedPath, purpose, 'file')

  if (stat.isDirectory()) {
    throw ipcPathError('EISDIR', `${purpose} failed: path points to a directory.`)
  }

  if (!stat.isFile()) {
    throw ipcPathError('EINVAL', `${purpose} failed: only regular files can be read.`)
  }

  const realPath = await realpathForIpc(fsImpl, resolvedPath, purpose)

  if (options.blockSensitive !== false) {
    rejectSensitiveFilePath(realPath, purpose)
  }

  const maxBytes = Number.isFinite(options.maxBytes) && Number(options.maxBytes) > 0 ? Number(options.maxBytes) : null

  if (maxBytes && stat.size > maxBytes) {
    throw ipcPathError('EFBIG', `${purpose} failed: file is too large (${stat.size} bytes; limit ${maxBytes} bytes).`)
  }

  try {
    await fsImpl.promises.access(resolvedPath, fs.constants.R_OK)
  } catch {
    throw ipcPathError('EACCES', `${purpose} failed: file is not readable.`)
  }

  return { realPath, resolvedPath, stat }
}

async function readFileDataUrlForIpc(
  filePath,
  options: {
    purpose?: string
    baseDir?: fs.PathOrFileDescriptor
    fs?: typeof fs
    blockSensitive?: boolean
    maxBytes?: number
    mimeType: string
  }
): Promise<string> {
  const fsImpl = options.fs || fs
  const { resolvedPath } = await resolveReadableFileForIpc(filePath, options)
  const data = await fsImpl.promises.readFile(resolvedPath)

  return `data:${options.mimeType};base64,${data.toString('base64')}`
}

export {
  ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES,
  buildWindowsOwnerOnlyAclCommand,
  clampDataUrlReadMaxMb,
  DATA_URL_READ_DEFAULT_MAX_MB,
  DATA_URL_READ_MAX_MAX_MB,
  DATA_URL_READ_MIN_MAX_MB,
  dataUrlReadMaxBytesFromMb,
  DEFAULT_FETCH_TIMEOUT_MS,
  enableBasicPasswordStoreEncryption,
  encryptDesktopSecret,
  readFileDataUrlForIpc,
  rejectUnsafePathSyntax,
  requireSecretFileProtection,
  resolveDirectoryForIpc,
  resolvePersistedRemoteToken,
  resolveReadableFileForIpc,
  resolveRequestedPathForIpc,
  resolveTimeoutMs,
  SAFE_STORAGE_ENCODING,
  SECRET_FILE_MODE,
  sensitiveFileBlockReason,
  TEXT_PREVIEW_SOURCE_MAX_BYTES,
  tightenSecretFileMode,
  writeSecretFileAtomic
}
