import { ChevronRightIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { describeEntry, formatMetricValue, formatRelativeTime } from '../activity/activityFormat'
import { getAuditLog } from '../api/client'
import type { AuditEntry } from '../api/types'
import { useT } from '../i18n'
import { ActivityDetailsDialog } from './ActivityDetailsDialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

const PAGE_SIZE = 25

function formatDateTime(value: string, locale: 'es' | 'en'): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')
}

interface ActivityTabProps {
  slug: string
  token: string
}

export function ActivityTab({ slug, token }: ActivityTabProps) {
  const { t, locale } = useT()
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [limit, setLimit] = useState(PAGE_SIZE)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<AuditEntry | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    getAuditLog(slug, token, limit)
      .then((log) => {
        if (!cancelled) setEntries(log.entries)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : t.common.loadFailed)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [slug, token, limit, t.common.loadFailed])

  function actionLabel(entry: AuditEntry): string {
    return t.activity.actions[entry.action] ?? t.activity.unknownAction
  }

  if (loading && entries.length === 0) {
    return (
      <div className="flex flex-col gap-3" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{t.common.loadingActivity}</p>
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    )
  }

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    )
  }

  if (entries.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">{t.common.noActivity}</p>
  }

  return (
    <div className="flex flex-col gap-3">
      <ul aria-label={t.activity.listLabel} className="flex flex-col divide-y">
        {entries.map((entry) => {
          const summary = describeEntry(entry)
          const absolute = formatDateTime(entry.created_at, locale)
          const relative = formatRelativeTime(entry.created_at, locale) ?? absolute
          const label = actionLabel(entry)
          return (
            <li key={entry.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="font-medium break-words">{label}</span>
                  {!summary.known && <span className="font-mono text-xs break-all text-muted-foreground">{entry.action}</span>}
                </div>
                <time dateTime={entry.created_at} title={absolute} className="shrink-0 text-xs text-muted-foreground">
                  {relative}
                </time>
              </div>

              {summary.metrics.length > 0 ? (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:flex sm:flex-wrap sm:gap-x-6">
                  {summary.metrics.map((metric) => (
                    <div key={metric.id} className="flex min-w-0 flex-col">
                      <dt className="text-xs text-muted-foreground">{t.activity.metrics[metric.id]}</dt>
                      <dd className="font-medium break-words tabular-nums">
                        {formatMetricValue(metric, locale, { yes: t.activity.yes, no: t.activity.no })}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p className="text-sm text-muted-foreground">{t.activity.noSummary}</p>
              )}

              <div className="flex items-center justify-between gap-3">
                <Badge variant="outline" title={t.activity.originLabel}>
                  {t.activity.origins[entry.actor] ?? entry.actor}
                </Badge>
                <Button
                  type="button"
                  variant="ghost"
                  className="h-11 px-3 sm:h-9"
                  aria-label={t.activity.viewDetails(label)}
                  onClick={() => setSelected(entry)}
                >
                  {t.activity.detailsTitle}
                  <ChevronRightIcon />
                </Button>
              </div>
            </li>
          )
        })}
      </ul>

      <div className="flex flex-col items-center gap-2 pt-2">
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {t.activity.showing(entries.length)}
        </p>
        {entries.length >= limit && (
          <Button
            type="button"
            variant="outline"
            className="h-11 w-full sm:h-9 sm:w-auto"
            onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
            disabled={loading}
          >
            {loading ? t.common.loading : t.common.loadMore}
          </Button>
        )}
      </div>

      <ActivityDetailsDialog
        entry={selected}
        actionLabel={selected ? actionLabel(selected) : ''}
        absoluteTime={selected ? formatDateTime(selected.created_at, locale) : ''}
        onClose={() => setSelected(null)}
      />
    </div>
  )
}
