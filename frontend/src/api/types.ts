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
  report_id: string
  created_at: string | null
  payload: ReportPayload
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
  last_run?: string | null
  [key: string]: unknown
}

export interface PendingEntry {
  action: ResolutionAction
  note?: string
}

export type PendingQueue = Record<string, PendingEntry>

