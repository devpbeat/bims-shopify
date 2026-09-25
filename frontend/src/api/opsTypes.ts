export type OpsCommand =
  | 'status'
  | 'wipe'
  | 'import'
  | 'dedupe'
  | 'rekey'
  | 'fix_tracking'
  | 'cleanup_no_stock'
  | 'sync'

export type OpsJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'

export type CleanupMode = 'draft' | 'delete'

export interface OpsRunOptions {
  confirm?: boolean
  apply?: boolean
  only_with_stock?: boolean
  limit?: number
  publish?: boolean
  force?: boolean
  auto_resolve?: boolean
  mode?: CleanupMode
  dry_run?: boolean
  full?: boolean
}

export interface OpsRunResponse {
  job_id: string
  status: OpsJobStatus
}

export interface OpsJob {
  id: string
  command: OpsCommand
  status: OpsJobStatus
  progress_done: number | null
  progress_total: number | null
  message: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface OpsJobDetail extends OpsJob {
  result: unknown
}
