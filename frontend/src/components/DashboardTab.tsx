import { useEffect, useState } from 'react'
import { getOpsJobs } from '../api/opsClient'
import type { OpsJob } from '../api/opsTypes'
import type { DashboardData, SyncStatus } from '../api/types'
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
  dashboard: DashboardData | null
  conflictsVisible: boolean
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
  dashboard,
  conflictsVisible,
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
  const [errorDismissed, setErrorDismissed] = useState(false)

  useEffect(() => {
    if (!adminToken) return
    getOpsJobs(adminToken, slug, 5)
      .then(setRecentJobs)
      .catch(() => {})
  }, [adminToken, slug])

  const localeTag = locale === 'es' ? 'es-AR' : 'en-US'
  const lastSync = dashboard?.last_sync.ran_at ?? syncStatus?.last_run_at
  const formattedSync = lastSync ? new Date(lastSync).toLocaleString(localeTag) : d.never
  const relativeSync = lastSync ? formatRelative(lastSync, locale) : null

  const lastSyncStatus = dashboard?.last_sync.status
  const syncHealthy = lastSyncStatus === 'ok'

  const totalIssues = duplicatesCount + unresolvedCount + mismatchesCount

  const lastError = dashboard?.last_error
  const lastErrorAt = dashboard?.last_error_at

  const autoImport = dashboard?.auto_import
  const catalog = dashboard?.catalog

  return (
    <div className="dash-tab">
      {lastError && !errorDismissed && (
        <div className="dash-error-banner">
          <div>
            <strong>{d.lastErrorTitle}</strong>
            <p className="muted">{lastError}</p>
            {lastErrorAt && (
              <p className="muted">
                {d.lastErrorAt}: {new Date(lastErrorAt).toLocaleString(localeTag)}
              </p>
            )}
          </div>
          <button type="button" className="btn-link" onClick={() => setErrorDismissed(true)}>
            {t.common.dismiss}
          </button>
        </div>
      )}

      <div className="dash-row">
        <div className="dash-card dash-card--sync">
          <p className="label">{d.syncStatus}</p>
          <div className="dash-sync-status">
            <span className={`dash-health-dot${syncHealthy ? ' dash-health-dot--ok' : ' dash-health-dot--warn'}`} />
            <span className="dash-sync-label">{syncHealthy ? d.healthy : d.needsAttention}</span>
          </div>
          <p className="muted">
            {d.lastSync}: <strong>{relativeSync ?? formattedSync}</strong>
          </p>
          {relativeSync && <p className="muted">{formattedSync}</p>}
          {dashboard?.last_sync.matched != null && (
            <p className="muted">
              {d.matched}: {dashboard.last_sync.matched} · {d.updated}: {dashboard.last_sync.updated}
            </p>
          )}
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
        <p className="label">{d.autoImportTitle}</p>
        {autoImport ? (
          <p className="muted">
            {autoImport.enabled ? d.autoImportOn : d.autoImportOff}
            {autoImport.enabled && (
              <>
                {' · '}
                {d.autoImportInterval(autoImport.interval_minutes)}
                {' · '}
                {autoImport.only_with_stock ? d.autoImportOnlyWithStock : d.autoImportAllProducts}
              </>
            )}
          </p>
        ) : (
          <p className="muted">{d.never}</p>
        )}
      </div>

      <div className="dash-card">
        <p className="label">{d.catalogTitle}</p>
        {catalog && catalog.shopify_products != null ? (
          <div className="dash-stat-row">
            <div className="dash-stat-tile dash-stat-tile--ok">
              <span className="dash-stat-count">{catalog.shopify_products}</span>
              <span className="dash-stat-label">{d.catalogProducts}</span>
            </div>
            <div className="dash-stat-tile dash-stat-tile--ok">
              <span className="dash-stat-count">{catalog.shopify_variants}</span>
              <span className="dash-stat-label">{d.catalogVariants}</span>
            </div>
            <div className="dash-stat-tile dash-stat-tile--ok">
              <span className="dash-stat-count">{catalog.matched_skus}</span>
              <span className="dash-stat-label">{d.catalogMatched}</span>
            </div>
            <div className="dash-stat-tile dash-stat-tile--warn">
              <span className="dash-stat-count">{catalog.bims_not_in_shopify}</span>
              <span className="dash-stat-label">{d.catalogNotInShopify}</span>
            </div>
          </div>
        ) : (
          <p className="muted">{catalog?.hint ?? d.catalogNoData}</p>
        )}
      </div>

      {conflictsVisible && (
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
      )}

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

function formatRelative(value: string, locale: 'es' | 'en'): string | null {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  const diffMs = Date.now() - date.getTime()
  const diffMinutes = Math.round(diffMs / 60000)
  if (diffMinutes < 1) return locale === 'es' ? 'hace instantes' : 'just now'
  if (diffMinutes < 60) {
    return locale === 'es' ? `hace ${diffMinutes} min` : `${diffMinutes} min ago`
  }
  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) {
    return locale === 'es' ? `hace ${diffHours} h` : `${diffHours} h ago`
  }
  const diffDays = Math.round(diffHours / 24)
  return locale === 'es' ? `hace ${diffDays} d` : `${diffDays} d ago`
}
