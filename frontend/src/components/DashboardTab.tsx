import { CircleAlertIcon, CircleCheckIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { formatRelativeTime } from '../activity/activityFormat'
import { getOpsJobs } from '../api/opsClient'
import type { OpsJob } from '../api/opsTypes'
import type { DashboardData, SyncStatus } from '../api/types'
import { useT } from '../i18n'
import { ResponsiveRecords } from './ResponsiveRecords'
import type { RecordColumn } from './ResponsiveRecords'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

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

interface MetricTileProps {
  label: string
  value: number | null
  tone?: 'default' | 'warn'
}

/** One catalog counter. The label wraps instead of truncating. */
function MetricTile({ label, value, tone = 'default' }: MetricTileProps) {
  return (
    <div
      className={cn(
        'flex flex-col-reverse justify-end gap-1 rounded-lg border bg-muted/30 p-3',
        tone === 'warn' && 'border-amber-500/40 bg-amber-500/5',
      )}
    >
      <dt className="text-xs leading-snug text-muted-foreground">{label}</dt>
      <dd className="text-2xl leading-none font-semibold tabular-nums">{value ?? '—'}</dd>
    </div>
  )
}

function IssueTile({ label, count, onClick }: { label: string; count: number; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex min-h-20 flex-col items-center justify-center gap-1 rounded-lg border p-3 text-center transition-colors hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
        count > 0 ? 'border-amber-500/40 bg-amber-500/5' : 'bg-muted/30',
      )}
    >
      <span className="text-2xl leading-none font-semibold tabular-nums">{count}</span>
      <span className="text-xs leading-snug text-muted-foreground">{label}</span>
    </button>
  )
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

  const localeTag = locale === 'es' ? 'es-419' : 'en-US'
  const lastSync = dashboard?.last_sync.ran_at ?? syncStatus?.last_run_at
  const formattedSync = lastSync ? new Date(lastSync).toLocaleString(localeTag) : d.never
  const relativeSync = lastSync ? formatRelativeTime(lastSync, locale) : null
  const syncHealthy = dashboard?.last_sync.status === 'ok'
  const totalIssues = duplicatesCount + unresolvedCount + mismatchesCount
  const { last_error: lastError, last_error_at: lastErrorAt, auto_import: autoImport, catalog } = dashboard ?? {}

  const jobColumns: RecordColumn<OpsJob>[] = [
    { id: 'command', header: t.operator.columns.command, cell: (job) => job.command },
    {
      id: 'status',
      header: t.operator.columns.status,
      cell: (job) => (
        <Badge variant={job.status === 'failed' || job.status === 'interrupted' ? 'destructive' : 'secondary'}>
          {(t.operator.jobStatus as Record<string, string>)[job.status] ?? job.status}
        </Badge>
      ),
    },
    {
      id: 'time',
      header: t.operator.columns.time,
      cell: (job) => <span className="text-muted-foreground">{new Date(job.created_at).toLocaleString(localeTag)}</span>,
    },
  ]

  return (
    <div className="flex flex-col gap-4">
      {lastError && !errorDismissed && (
        <Alert variant="destructive">
          <CircleAlertIcon />
          <AlertTitle>{d.lastErrorTitle}</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span className="break-words">{lastError}</span>
            {lastErrorAt && (
              <span>
                {d.lastErrorAt}: {new Date(lastErrorAt).toLocaleString(localeTag)}
              </span>
            )}
            <Button type="button" variant="outline" className="h-11 self-start sm:h-9" onClick={() => setErrorDismissed(true)}>
              {t.common.dismiss}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card size="sm">
          <CardHeader>
            <CardTitle>{d.syncStatus}</CardTitle>
          </CardHeader>
          <CardContent className="gap-2">
            <p className="flex items-center gap-2 font-medium">
              {syncHealthy ? (
                <CircleCheckIcon className="size-4 text-emerald-600" aria-hidden="true" />
              ) : (
                <CircleAlertIcon className="size-4 text-amber-600" aria-hidden="true" />
              )}
              {syncHealthy ? d.healthy : d.needsAttention}
            </p>
            <p className="text-muted-foreground">
              {d.lastSync}: <span className="font-medium text-foreground">{relativeSync ?? formattedSync}</span>
            </p>
            {relativeSync && <p className="text-xs text-muted-foreground">{formattedSync}</p>}
            {dashboard?.last_sync.matched != null && (
              <p className="text-muted-foreground">
                {d.matched}: {dashboard.last_sync.matched} · {d.updated}: {dashboard.last_sync.updated}
              </p>
            )}
            <p className="text-muted-foreground">
              {dashboard?.last_push
                ? d.lastPush(dashboard.last_push.updated, new Date(dashboard.last_push.ran_at).toLocaleString(localeTag))
                : d.lastPushNone}
            </p>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <Card size="sm">
            <CardHeader>
              <CardTitle>{d.pendingTitle}</CardTitle>
            </CardHeader>
            <CardContent>
              <p className={pendingCount === 0 ? 'text-muted-foreground' : 'font-medium text-amber-700'}>
                {pendingCount === 0 ? d.pendingNone : d.pendingCount(pendingCount)}
              </p>
            </CardContent>
          </Card>

          <Card size="sm">
            <CardHeader>
              <CardTitle>{d.autoImportTitle}</CardTitle>
            </CardHeader>
            <CardContent>
              {autoImport ? (
                <p className="flex flex-wrap items-center gap-2">
                  <Badge variant={autoImport.enabled ? 'default' : 'secondary'}>
                    {autoImport.enabled ? d.autoImportOn : d.autoImportOff}
                  </Badge>
                  {autoImport.enabled && (
                    <span className="text-muted-foreground">
                      {d.autoImportInterval(autoImport.interval_minutes)} ·{' '}
                      {autoImport.only_with_stock ? d.autoImportOnlyWithStock : d.autoImportAllProducts}
                    </span>
                  )}
                </p>
              ) : (
                <p className="text-muted-foreground">{d.never}</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <Card size="sm">
        <CardHeader>
          <CardTitle>{d.catalogTitle}</CardTitle>
        </CardHeader>
        <CardContent>
          {catalog && catalog.shopify_products != null ? (
            <>
              <dl className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
                <MetricTile label={d.catalogEligibleProducts} value={catalog.bims_eligible_products} />
                <MetricTile label={d.catalogWithStock} value={catalog.bims_with_stock} />
                <MetricTile label={d.catalogProducts} value={catalog.shopify_products} />
                <MetricTile label={d.catalogVariants} value={catalog.shopify_variants} />
                <MetricTile label={d.catalogMatched} value={catalog.matched_skus} />
                <MetricTile label={d.catalogNotInShopify} value={catalog.bims_not_in_shopify} tone="warn" />
              </dl>
              {catalog.checked_at && (
                <p className="text-xs text-muted-foreground">
                  {d.catalogCheckedAt(
                    formatRelativeTime(catalog.checked_at, locale) ?? new Date(catalog.checked_at).toLocaleString(localeTag),
                  )}
                </p>
              )}
            </>
          ) : (
            <p className="text-muted-foreground">{catalog?.hint ?? d.catalogNoData}</p>
          )}
        </CardContent>
      </Card>

      {conflictsVisible && (
        <Card size="sm">
          <CardHeader>
            <CardTitle>{d.issuesTitle}</CardTitle>
          </CardHeader>
          <CardContent>
            {totalIssues === 0 ? (
              <p className="text-muted-foreground">{d.noConflicts}</p>
            ) : (
              <div className="grid grid-cols-3 gap-3">
                <IssueTile label={d.statDuplicates} count={duplicatesCount} onClick={() => onNavigate('duplicates')} />
                <IssueTile label={d.statUnresolved} count={unresolvedCount} onClick={() => onNavigate('unresolved')} />
                <IssueTile label={d.statMismatches} count={mismatchesCount} onClick={() => onNavigate('mismatches')} />
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {adminToken && (
        <Card size="sm">
          <CardHeader>
            <CardTitle>{d.recentJobsTitle}</CardTitle>
          </CardHeader>
          <CardContent>
            {recentJobs.length === 0 ? (
              <p className="text-muted-foreground">{d.noJobs}</p>
            ) : (
              <ResponsiveRecords rows={recentJobs} columns={jobColumns} getKey={(job) => job.id} label={d.recentJobsTitle} />
            )}
            <Button type="button" variant="outline" className="h-11 w-full sm:h-9 sm:w-auto sm:self-start" onClick={() => onNavigate('operations')}>
              {d.viewAll}
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
