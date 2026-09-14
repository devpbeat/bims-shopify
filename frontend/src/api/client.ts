import type { RekeyReport, ResolutionRequest, ResolutionResult, SyncStatus } from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function portalFetch<T>(slug: string, token: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/portal/${encodeURIComponent(slug)}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    },
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // ignore non-JSON error bodies
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

export function getRekeyReport(slug: string, token: string): Promise<RekeyReport> {
  return portalFetch<RekeyReport>(slug, token, '/rekey-report')
}

export function getSyncStatus(slug: string, token: string): Promise<SyncStatus> {
  return portalFetch<SyncStatus>(slug, token, '/sync-status')
}

export function postRekeyResolutions(
  slug: string,
  token: string,
  resolutions: ResolutionRequest[],
): Promise<{ results: ResolutionResult[] }> {
  return portalFetch<{ results: ResolutionResult[] }>(slug, token, '/rekey-resolutions', {
    method: 'POST',
    body: JSON.stringify({ resolutions }),
  })
}
