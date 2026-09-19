import { ApiError } from './client'
import type {
  OpsCommand,
  OpsJob,
  OpsJobDetail,
  OpsRunOptions,
  OpsRunResponse,
} from './opsTypes'

async function opsFetch<T>(adminToken: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/ops${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${adminToken}`,
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

export function runOpsCommand(
  adminToken: string,
  slug: string,
  command: OpsCommand,
  options?: OpsRunOptions,
): Promise<OpsRunResponse> {
  return opsFetch<OpsRunResponse>(adminToken, `/${encodeURIComponent(slug)}/run`, {
    method: 'POST',
    body: JSON.stringify({ command, options: options ?? {} }),
  })
}

export function getOpsJobs(adminToken: string, slug: string, limit = 20): Promise<OpsJob[]> {
  return opsFetch<OpsJob[]>(adminToken, `/${encodeURIComponent(slug)}/jobs?limit=${encodeURIComponent(String(limit))}`)
}

export function getOpsJob(adminToken: string, slug: string, jobId: string): Promise<OpsJobDetail> {
  return opsFetch<OpsJobDetail>(adminToken, `/${encodeURIComponent(slug)}/jobs/${encodeURIComponent(jobId)}`)
}
