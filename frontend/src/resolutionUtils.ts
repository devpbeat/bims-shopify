import type { Resolution } from './api/types'
import type { Messages } from './i18n/types'

/** Latest resolution for a variant, if the report already has one. */
export function findResolution(resolutions: Resolution[], variantId: string): Resolution | undefined {
  return resolutions.find((resolution) => resolution.variant_id === variantId)
}

/** A row can be queued again unless it already has a non-failed resolution. */
export function isLocked(resolutions: Resolution[], variantId: string): boolean {
  const resolution = findResolution(resolutions, variantId)
  return resolution !== undefined && resolution.status !== 'failed'
}

export function statusLabel(status: string, t: Messages): string {
  switch (status) {
    case 'applied':
      return t.status.applied
    case 'recorded':
      return t.status.recorded
    case 'failed':
      return t.status.failed
    case 'already_resolved':
      return t.status.already_resolved
    default:
      return status
  }
}
