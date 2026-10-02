export type ResolutionAction = 'keep' | 'delete' | 'approve_sku' | 'ignore'

export type ResolutionStatus = 'recorded' | 'applied' | 'failed' | 'already_resolved'

export interface Resolution {
  variant_id: string
  action: ResolutionAction
  status: ResolutionStatus
  error: string | null
  note?: string | null
  created_at?: string | null
}

export interface DuplicateTargetRow {
  product_title: string
  old_sku: string
  new_sku: string
  bims_name: string
  variant_id: string
  product_id: string
}

export interface UnresolvedRow {
  product_title: string
  sku: string
  variant_id: string
}

export interface NameMismatchRow {
  product_title: string
  bims_name: string
  old_sku: string
  new_sku: string
  variant_id: string
}

export interface ReportPayload {
  planned_rewrites?: unknown[]
  duplicate_target?: DuplicateTargetRow[]
  unresolved?: UnresolvedRow[]
  name_mismatch?: NameMismatchRow[]
}

export interface RekeyReport {
  obsolete: boolean
  report_id?: string
  created_at: string | null
  payload: ReportPayload | null
  resolutions: Resolution[]
}

export interface ResolutionRequest {
  variant_id: string
  action: ResolutionAction
  note?: string
}

export interface ResolutionResult {
  variant_id: string
  action: string
  status: ResolutionStatus
  error: string | null
}

export interface SyncStatus {
  last_run_at?: string | null
  last_error?: string | null
  last_error_at?: string | null
  last_run_summary?: Record<string, unknown>
  [key: string]: unknown
}

export interface DashboardLastSync {
  ran_at: string | null
  status: string | null
  matched: number | null
  updated: number | null
  duration_seconds: number | null
}

export interface DashboardAutoImport {
  enabled: boolean
  interval_minutes: number
  only_with_stock: boolean
}

export interface DashboardCatalog {
  bims_eligible_products: number | null
  bims_with_stock: number | null
  shopify_products: number | null
  shopify_variants: number | null
  matched_skus: number | null
  bims_not_in_shopify: number | null
  checked_at?: string | null
  hint?: string
}

export interface DashboardLastPush {
  updated: number
  ran_at: string
}

export interface DashboardData {
  slug: string
  last_sync: DashboardLastSync
  last_push: DashboardLastPush | null
  last_error: string | null
  last_error_at: string | null
  auto_import: DashboardAutoImport
  catalog: DashboardCatalog
}

export interface PendingEntry {
  action: ResolutionAction
  note?: string
}

export type PendingQueue = Record<string, PendingEntry>

export type AuditActor = 'admin' | 'portal' | 'system' | 'shopify'

export interface AuditEntry {
  id: number
  actor: AuditActor
  action: string
  entity: string
  entity_id: string | null
  payload: Record<string, unknown> | null
  created_at: string
}

export interface AuditLog {
  entries: AuditEntry[]
}

