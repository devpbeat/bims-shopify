import type {
  AuditLog,
  DashboardData,
  PaymentExportFile,
  PaymentReport,
  PaymentReportQuery,
  PaymentsConfig,
  PaymentsConfigUpdate,
  PaymentsTestResult,
  RekeyReport,
  ResolutionRequest,
  ResolutionResult,
  SyncStatus,
} from './types'

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

export interface LoginResponse {
  token: string
  expires_in: number
}

export async function login(slug: string, email: string, password: string): Promise<LoginResponse> {
  const response = await fetch(`/api/portal/${encodeURIComponent(slug)}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
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

  return (await response.json()) as LoginResponse
}

export function getRekeyReport(slug: string, token: string): Promise<RekeyReport> {
  return portalFetch<RekeyReport>(slug, token, '/rekey-report')
}

export function getSyncStatus(slug: string, token: string): Promise<SyncStatus> {
  return portalFetch<SyncStatus>(slug, token, '/sync-status')
}

export function getDashboard(slug: string, token: string): Promise<DashboardData> {
  return portalFetch<DashboardData>(slug, token, '/dashboard')
}

export function getAuditLog(slug: string, token: string, limit: number): Promise<AuditLog> {
  return portalFetch<AuditLog>(slug, token, `/audit?limit=${encodeURIComponent(String(limit))}`)
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

export function getPaymentsConfig(slug: string, token: string): Promise<PaymentsConfig> {
  return portalFetch<PaymentsConfig>(slug, token, '/payments-config')
}

export function putPaymentsConfig(
  slug: string,
  token: string,
  update: PaymentsConfigUpdate,
): Promise<PaymentsConfig> {
  return portalFetch<PaymentsConfig>(slug, token, '/payments-config', {
    method: 'PUT',
    body: JSON.stringify({ provider: 'bancard_vpos', ...update }),
  })
}

export function testPaymentsConnection(slug: string, token: string): Promise<PaymentsTestResult> {
  return portalFetch<PaymentsTestResult>(slug, token, '/payments-config/test-connection', {
    method: 'POST',
  })
}

function reportQueryString(query: PaymentReportQuery, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...query, ...extra })) {
    if (value) params.set(key, value)
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

export function getPaymentsReport(
  slug: string,
  token: string,
  query: PaymentReportQuery,
  limit: number,
  offset: number,
): Promise<PaymentReport> {
  const qs = reportQueryString(query, { limit: String(limit), offset: String(offset) })
  return portalFetch<PaymentReport>(slug, token, `/payments/report${qs}`)
}

/** Extracts the filename from a Content-Disposition header, or null when absent. */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const match = /filename="?([^";]+)"?/i.exec(header)
  return match ? match[1] : null
}

export async function downloadPaymentsExport(
  slug: string,
  token: string,
  query: PaymentReportQuery,
): Promise<PaymentExportFile> {
  const response = await fetch(
    `/api/portal/${encodeURIComponent(slug)}/payments/export${reportQueryString(query)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  )
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
  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(response.headers.get('Content-Disposition')) ?? 'payments.xlsx',
    truncated: response.headers.get('X-Report-Truncated') === 'true',
  }
}
