import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, downloadPaymentsExport, getPaymentsReport } from '../api/client'
import type { PaymentReport, PaymentReportQuery, PaymentReportRow } from '../api/types'
import { useT } from '../i18n'
import {
  formatGuaranies,
  isoDateTime,
  PAYMENT_PROVIDERS,
  PAYMENT_STATUSES,
} from '../paymentsReportUtils'
import { Field } from './Field'
import { PaymentsPerDayChart } from './PaymentsPerDayChart'
import { ResponsiveRecords } from './ResponsiveRecords'
import type { RecordColumn } from './ResponsiveRecords'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 50
const ALL = 'all'
const touch = 'h-11 sm:h-9'

const STATUS_TONE: Record<string, string> = {
  paid: 'border-l-emerald-500',
  pending: 'border-l-amber-500',
  failed: 'border-l-destructive',
  cancelled: 'border-l-muted-foreground',
}

interface PaymentsReportTabProps {
  slug: string
  token: string
}

export function PaymentsReportTab({ slug, token }: PaymentsReportTabProps) {
  const { t } = useT()
  const text = t.paymentsReport
  const [draft, setDraft] = useState({ date_from: '', date_to: '', status: '', provider: '' })
  const [query, setQuery] = useState<PaymentReportQuery>({})
  const [offset, setOffset] = useState(0)
  const [report, setReport] = useState<PaymentReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    getPaymentsReport(slug, token, query, PAGE_SIZE, offset)
      .then((data) => {
        if (cancelled) return
        setReport(data)
        // Mirror what the server actually applied (it fills the default window).
        setDraft({
          date_from: data.filters.date_from ?? '',
          date_to: data.filters.date_to ?? '',
          status: data.filters.status ?? '',
          provider: data.filters.provider ?? '',
        })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 401) setError(text.sessionExpired)
        else if (err instanceof ApiError && err.status === 422) setError(err.message)
        else setError(text.loadFailed)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [slug, token, query, offset, text.loadFailed, text.sessionExpired])

  function handleApply(event: FormEvent) {
    event.preventDefault()
    setOffset(0)
    setTruncated(false)
    setDownloadError(null)
    setQuery({
      date_from: draft.date_from || undefined,
      date_to: draft.date_to || undefined,
      status: draft.status || undefined,
      provider: draft.provider || undefined,
    })
  }

  async function handleDownload() {
    if (!report) return
    setDownloading(true)
    setDownloadError(null)
    setTruncated(false)
    try {
      // Export exactly what the server reports as applied, not the unsent draft.
      const applied = report.filters
      const file = await downloadPaymentsExport(slug, token, {
        date_from: applied.date_from ?? undefined,
        date_to: applied.date_to ?? undefined,
        status: applied.status ?? undefined,
        provider: applied.provider ?? undefined,
      })
      const url = URL.createObjectURL(file.blob)
      const link = document.createElement('a')
      link.href = url
      link.download = file.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setTruncated(file.truncated)
    } catch (err) {
      setDownloadError(err instanceof ApiError && err.status === 422 ? err.message : text.downloadFailed)
    } finally {
      setDownloading(false)
    }
  }

  const statusLabels: Record<string, string> = {
    pending: text.statusPending,
    paid: text.statusPaid,
    failed: text.statusFailed,
    cancelled: text.statusCancelled,
  }
  const statusText = (status: string) => statusLabels[status] ?? status

  const filtersForm = (
    <form onSubmit={handleApply}>
      <Card size="sm">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <CardDescription>{text.intro}</CardDescription>
        </CardHeader>
        <CardContent className="gap-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label={text.dateFrom}>
              {(control) => (
                <Input {...control} className={touch} type="date" value={draft.date_from} onChange={(e) => setDraft({ ...draft, date_from: e.target.value })} />
              )}
            </Field>
            <Field label={text.dateTo}>
              {(control) => (
                <Input {...control} className={touch} type="date" value={draft.date_to} onChange={(e) => setDraft({ ...draft, date_to: e.target.value })} />
              )}
            </Field>
            <Field label={text.status}>
              {(control) => (
                <Select value={draft.status || ALL} onValueChange={(value) => setDraft({ ...draft, status: value === ALL ? '' : value })}>
                  <SelectTrigger {...control} className={cn(touch, 'w-full')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{text.allStatuses}</SelectItem>
                    {PAYMENT_STATUSES.map((s) => (
                      <SelectItem key={s} value={s}>{statusText(s)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </Field>
            <Field label={text.provider}>
              {(control) => (
                <Select value={draft.provider || ALL} onValueChange={(value) => setDraft({ ...draft, provider: value === ALL ? '' : value })}>
                  <SelectTrigger {...control} className={cn(touch, 'w-full')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{text.allProviders}</SelectItem>
                    {PAYMENT_PROVIDERS.map((p) => (
                      <SelectItem key={p} value={p}>{p}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </Field>
          </div>
          <Button type="submit" className={cn(touch, 'w-full sm:w-auto sm:self-start')} disabled={loading}>
            {text.apply}
          </Button>
          {report && (
            <p className="text-xs break-words text-muted-foreground">
              {text.applied}: {report.filters.date_from ?? '—'} → {report.filters.date_to ?? '—'} · {text.status}:{' '}
              {report.filters.status ? statusText(report.filters.status) : text.appliedNone} · {text.provider}:{' '}
              {report.filters.provider ?? text.appliedNone}
            </p>
          )}
        </CardContent>
      </Card>
    </form>
  )

  if (!report && loading) {
    return (
      <div className="flex flex-col gap-3" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{text.loading}</p>
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  if (!report) {
    return (
      <div className="flex flex-col gap-4">
        {filtersForm}
        <Alert variant="destructive" role="alert">
          <AlertDescription>{error ?? text.loadFailed}</AlertDescription>
        </Alert>
      </div>
    )
  }

  const from = report.total === 0 ? 0 : report.offset + 1
  const to = Math.min(report.offset + report.rows.length, report.total)

  const columns: RecordColumn<PaymentReportRow>[] = [
    { id: 'order', header: text.tableOrder, cell: (row) => row.order_id },
    { id: 'provider', header: text.tableProvider, cell: (row) => row.provider },
    { id: 'amount', header: text.tableAmount, numeric: true, cell: (row) => `${formatGuaranies(row.amount)} ${row.currency}` },
    { id: 'status', header: text.tableStatus, cell: (row) => <Badge variant={row.status === 'failed' ? 'destructive' : 'secondary'}>{statusText(row.status)}</Badge> },
    { id: 'reference', header: text.tableReference, cell: (row) => <span className="break-all">{row.provider_reference ?? '—'}</span> },
    { id: 'created', header: text.tableCreated, cell: (row) => isoDateTime(row.created_at) },
  ]

  return (
    <div className="flex flex-col gap-4">
      {filtersForm}
      {error && (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-5" aria-busy={loading}>
        <div className="flex flex-col-reverse justify-end gap-1 rounded-lg border border-l-4 border-l-primary bg-card p-3">
          <dt className="text-xs text-muted-foreground">{text.total}</dt>
          <dd className="text-xl font-semibold tabular-nums">{report.total}</dd>
        </div>
        {report.by_status.map((s) => (
          <div key={s.status} className={cn('flex flex-col gap-1 rounded-lg border border-l-4 bg-card p-3', STATUS_TONE[s.status])}>
            <dt className="text-xs text-muted-foreground">{statusText(s.status)}</dt>
            <dd className="flex flex-col">
              <span className="text-lg font-semibold tabular-nums break-words">{formatGuaranies(s.total_amount)} PYG</span>
              <span className="text-xs text-muted-foreground">{s.count} {text.tileCount}</span>
            </dd>
          </div>
        ))}
      </dl>

      <Card size="sm">
        <CardHeader>
          <CardTitle>{text.chartTitle}</CardTitle>
        </CardHeader>
        <CardContent>
          <PaymentsPerDayChart byDay={report.by_day} dateFrom={report.filters.date_from} dateTo={report.filters.date_to} />
        </CardContent>
      </Card>

      <Card size="sm">
        <CardContent className="gap-4">
          <Button type="button" variant="outline" className={cn(touch, 'w-full sm:w-auto sm:self-start')} disabled={downloading || report.total === 0} onClick={handleDownload}>
            {downloading ? text.downloading : text.download}
          </Button>
          {truncated && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{text.truncated}</AlertDescription>
            </Alert>
          )}
          {downloadError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{downloadError}</AlertDescription>
            </Alert>
          )}

          {report.rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">{text.empty}</p>
          ) : (
            <ResponsiveRecords rows={report.rows} columns={columns} getKey={(row) => row.id} label={text.title} />
          )}

          <nav aria-label={text.title} className="flex items-center justify-between gap-2">
            <Button type="button" variant="outline" className={touch} disabled={loading || report.offset === 0} onClick={() => setOffset(Math.max(0, report.offset - PAGE_SIZE))}>
              {text.previous}
            </Button>
            <span className="text-center text-xs text-muted-foreground" aria-live="polite">
              {text.pageRange.replace('{from}', String(from)).replace('{to}', String(to)).replace('{total}', String(report.total))}
            </span>
            <Button type="button" variant="outline" className={touch} disabled={loading || report.offset + report.rows.length >= report.total} onClick={() => setOffset(report.offset + PAGE_SIZE)}>
              {text.next}
            </Button>
          </nav>
        </CardContent>
      </Card>
    </div>
  )
}
