import type { SyncStatus } from '../api/types'

interface PortalHeaderProps {
  slug: string
  reportDate: string | null
  syncStatus: SyncStatus | null
  onLogout: () => void
}

function formatDate(value: string | null | undefined): string {
  if (!value) return 'Never'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export function PortalHeader({ slug, reportDate, syncStatus, onLogout }: PortalHeaderProps) {
  return (
    <header className="portal-header">
      <div>
        <h1>{slug}</h1>
        <p className="muted">Rekey conflict resolution</p>
      </div>
      <div className="portal-header-meta">
        <div>
          <span className="label">Report date</span>
          <span>{formatDate(reportDate)}</span>
        </div>
        <div>
          <span className="label">Last sync</span>
          <span>{formatDate(syncStatus?.last_run as string | undefined)}</span>
        </div>
        <button type="button" className="btn btn-secondary" onClick={onLogout}>
          Log out
        </button>
      </div>
    </header>
  )
}
