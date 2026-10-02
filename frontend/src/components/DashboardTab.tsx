import { useEffect, useState } from 'react'
import { getOpsJobs } from '../api/opsClient'
import type { OpsJob } from '../api/opsTypes'
import type { SyncStatus } from '../api/types'
import { useT } from '../i18n'

interface StatTileProps {
  label: string
  count: number
  onClick: () => void
}

function StatTile({ label, count, onClick }: StatTileProps) {
  const hasIssues = count > 0
  return (
    <button
      type="button"
      className={`dash-stat-tile${hasIssues ? ' dash-stat-tile--warn' : ' dash-stat-tile--ok'}`}
      onClick={onClick}
    >
      <span className="dash-stat-count">{count}</span>
      <span className="dash-stat-label">{label}</span>
    </button>
  )
}

function JobStatusDot({ status }: { status: string }) {
  return <span className={`dash-job-dot dash-job-dot--${status}`} />
}

interface DashboardTabProps {
  slug: string
  syncStatus: SyncStatus | null
  duplicatesCount: number
  unresolvedCount: number
  mismatchesCount: number
  pendingCount: number
  adminToken: string | null
  onNavigate: (tab: 'duplicates' | 'unresolved' | 'mismatches' | 'operations') => void
}

export function DashboardTab({
  slug,
  syncStatus,
  duplicatesCount,
  unresolvedCount,
  mismatchesCount,
  pendingCount,
  adminToken,
  onNavigate,
}: DashboardTabProps) {
  const { t, locale } = useT()
  const d = t.dashboard
  const [recentJobs, setRecentJobs] = useState<OpsJob[]>([])

  useEffect(() => {
    if (!adminToken) return
    getOpsJobs(adminToken, slug, 5)
      .then(setRecentJobs)
      .catch(() => {})
  }, [adminToken, slug])

  const lastSync = syncStatus?.last_run
  const formattedSync = lastSync
    ? new Date(lastSync).toLocaleString(locale === 'es' ? 'es-AR' : 'en-US')
    : d.never

  const totalIssues = duplicatesCount + unresolvedCount + mismatchesCount
  const syncHealthy = !!lastSync

  return (
    <div className="dash-tab">
      <div className="dash-row">
        <div className="dash-card dash-card--sync">
          <p className="label">{d.syncStatus}</p>
          <div className="dash-sync-status">
            <span className={`dash-health-dot${syncHealthy ? ' dash-health-dot--ok' : ' dash-health-dot--warn'}`} />
            <span className="dash-sync-label">{syncHealthy ? d.healthy : d.needsAttention}</span>
          </div>
          <p className="muted">
            {d.lastSync}: <strong>{formattedSync}</strong>
          </p>
        </div>

        <div className="dash-card dash-card--pending">
          <p className="label">{d.pendingTitle}</p>
          {pendingCount === 0 ? (
            <p className="dash-pending-none">{d.pendingNone}</p>
          ) : (
            <p className="dash-pending-count">{d.pendingCount(pendingCount)}</p>
          )}
        </div>
      </div>

      <div className="dash-card">
        <p className="label">{d.issuesTitle}</p>
        {totalIssues === 0 ? (
          <p className="empty-state" style={{ padding: '12px 0' }}>{d.healthy} — no open issues</p>
        ) : (
          <div className="dash-stat-row">
            <StatTile label={d.statDuplicates} count={duplicatesCount} onClick={() => onNavigate('duplicates')} />
            <StatTile label={d.statUnresolved} count={unresolvedCount} onClick={() => onNavigate('unresolved')} />
            <StatTile label={d.statMismatches} count={mismatchesCount} onClick={() => onNavigate('mismatches')} />
          </div>
        )}
      </div>

      {adminToken && (
        <div className="dash-card">
          <div className="dash-card-header">
            <p className="label">{d.recentJobsTitle}</p>
            <button type="button" className="btn-link" onClick={() => onNavigate('operations')}>
              {d.viewAll}
            </button>
          </div>
          {recentJobs.length === 0 ? (
            <p className="muted">{d.noJobs}</p>
          ) : (
            <table className="ops-history-table">
              <thead>
                <tr>
                  <th>{t.operator.columns.command}</th>
                  <th>{t.operator.columns.status}</th>
                  <th>{t.operator.columns.time}</th>
                </tr>
              </thead>
              <tbody>
                {recentJobs.map((job) => (
                  <tr key={job.id}>
                    <td>{job.command}</td>
                    <td>
                      <JobStatusDot status={job.status} />
                      <span className={`badge badge-ops-${job.status}`}>
                        {(t.operator.jobStatus as Record<string, string>)[job.status] ?? job.status}
                      </span>
                    </td>
                    <td className="muted">{new Date(job.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
