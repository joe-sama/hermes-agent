const TARGET_TEXT_MAX_LENGTH = 160

export interface SecretRequestMetadata {
  kind?: 'computer_use'
  target?: SecretTargetMetadata
  transient?: true
}

export interface SecretTargetMetadata {
  appName?: string
  pid?: number
  title?: string
  windowId?: number
}

const safeTargetText = (value: unknown): string | undefined => {
  if (typeof value !== 'string') {
    return undefined
  }

  const withoutControls = Array.from(value, character => {
    const codePoint = character.codePointAt(0) ?? 0

    return codePoint <= 31 || (codePoint >= 127 && codePoint <= 159) ? ' ' : character
  }).join('')

  const normalized = withoutControls.replace(/\s+/g, ' ').trim().slice(0, TARGET_TEXT_MAX_LENGTH)

  return normalized || undefined
}

const safeTargetInteger = (value: unknown): number | undefined => {
  const parsed =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && /^\d+$/.test(value.trim())
        ? Number(value)
        : Number.NaN

  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined
}

/**
 * Keep only the non-secret metadata a renderer needs to identify the target.
 * The gateway payload is an untrusted wire object, so raw `value`, `text`,
 * `secret`, and all unknown fields are deliberately dropped at this boundary.
 */
export function normalizeSecretRequestMetadata(value: unknown): SecretRequestMetadata | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined
  }

  const raw = value as Record<string, unknown>

  const rawTarget =
    raw.target && typeof raw.target === 'object' && !Array.isArray(raw.target)
      ? (raw.target as Record<string, unknown>)
      : undefined

  const target = rawTarget
    ? {
        appName: safeTargetText(rawTarget.app_name),
        pid: safeTargetInteger(rawTarget.pid),
        title: safeTargetText(rawTarget.title),
        windowId: safeTargetInteger(rawTarget.window_id)
      }
    : undefined

  const safeTarget = target && Object.values(target).some(field => field !== undefined) ? target : undefined

  const metadata: SecretRequestMetadata = {
    kind: raw.kind === 'computer_use' ? 'computer_use' : undefined,
    target: safeTarget,
    transient: raw.transient === true ? true : undefined
  }

  return Object.values(metadata).some(field => field !== undefined) ? metadata : undefined
}

export function formatSecretTarget(target: SecretTargetMetadata | undefined): string {
  if (!target) {
    return ''
  }

  const named = [target.appName, target.title].filter(
    (part, index, parts): part is string => Boolean(part) && parts.indexOf(part) === index
  )

  if (named.length) {
    return named.join(' — ')
  }

  const identifiers = [
    target.pid === undefined ? '' : `PID ${target.pid}`,
    target.windowId === undefined ? '' : `window ${target.windowId}`
  ].filter(Boolean)

  return identifiers.join(', ')
}
