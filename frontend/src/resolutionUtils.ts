import type { Resolution } from './api/types'

/** Latest resolution for a variant, if the report already has one. */
export function findResolution(resolutions: Resolution[], variantId: string): Resolution | undefined {
  return resolutions.find((resolution) => resolution.variant_id === variantId)
}

/** A row can be queued again unless it already has a non-failed resolution. */
export function isLocked(resolutions: Resolution[], variantId: string): boolean {
  const resolution = findResolution(resolutions, variantId)
  return resolution !== undefined && resolution.status !== 'failed'
}

export function statusLabel(status: string): string {
  switch (status) {
    case 'applied':
      return 'Applied'
    case 'recorded':
      return 'Recorded'
    case 'failed':
      return 'Failed'
    case 'already_resolved':
      return 'Already resolved'
    default:
      return status
  }
}
