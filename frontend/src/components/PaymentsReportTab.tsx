import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, downloadPaymentsExport, getPaymentsReport } from '../api/client'
import type { PaymentReport, PaymentReportQuery } from '../api/types'
import { useT } from '../i18n'
import {
  formatGuaranies,
  isoDateTime,
  PAYMENT_PROVIDERS,
  PAYMENT_STATUSES,
} from '../paymentsReportUtils'
import { PaymentsPerDayChart } from './PaymentsPerDayChart'

const PAGE_SIZE = 50

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
    <form className="ops-card" onSubmit={handleApply}>
      <h3>{text.title}</h3>
      <p className="muted">{text.intro}</p>
      <div className="payments-filters">
        <label className="ops-field">
          {text.dateFrom}
          <input type="date" value={draft.date_from} onChange={(e) => setDraft({ ...draft, date_from: e.target.value })} />
        </label>
        <label className="ops-field">
          {text.dateTo}
          <input type="date" value={draft.date_to} onChange={(e) => setDraft({ ...draft, date_to: e.target.value })} />
        </label>
        <label className="ops-field">
          {text.status}
          <select value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
            <option value="">{text.allStatuses}</option>
            {PAYMENT_STATUSES.map((s) => (
              <option key={s} value={s}>{statusText(s)}</option>
            ))}
          </select>
        </label>
        <label className="ops-field">
          {text.provider}
          <select value={draft.provider} onChange={(e) => setDraft({ ...draft, provider: e.target.value })}>
            <option value="">{text.allProviders}</option>
            {PAYMENT_PROVIDERS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
        <button type="submit" className="btn btn-primary" disabled={loading}>{text.apply}</button>
      </div>
      {report && (
        <p className="muted">
          {text.applied}: {report.filters.date_from ?? '—'} → {report.filters.date_to ?? '—'} · {text.status}:{' '}
          {report.filters.status ? statusText(report.filters.status) : text.appliedNone} · {text.provider}:{' '}
          {report.filters.provider ?? text.appliedNone}
        </p>
      )}
    </form>
  )

  if (!report && loading) return <p className="muted">{text.loading}</p>

  if (!report) {
    return (
      <div className="ops-tab">
        {filtersForm}
        <p className="error-text" role="alert">{error ?? text.loadFailed}</p>
      </div>
    )
  }

  const from = report.total === 0 ? 0 : report.offset + 1
  const to = Math.min(report.offset + report.rows.length, report.total)

  return (
    <div className="ops-tab">
      {filtersForm}
      {error && <p className="error-text" role="alert">{error}</p>}

      <div className="payments-tiles" aria-busy={loading}>
        <div className="payments-tile">
          <span className="label">{text.total}</span>
          <span className="payments-tile-value">{report.total}</span>
        </div>
        {report.by_status.map((s) => (
          <div key={s.status} className={`payments-tile payments-tile-${s.status}`}>
            <span className="label">{statusText(s.status)}</span>
            <span className="payments-tile-value">{formatGuaranies(s.total_amount)} PYG</span>
            <span className="muted">{s.count} {text.tileCount}</span>
          </div>
        ))}
      </div>

      <section className="ops-card">
        <h3>{text.chartTitle}</h3>
        <PaymentsPerDayChart byDay={report.by_day} dateFrom={report.filters.date_from} dateTo={report.filters.date_to} />
      </section>

      <section className="ops-card">
        <div>
          <button type="button" className="btn btn-secondary" disabled={downloading || report.total === 0} onClick={handleDownload}>
            {downloading ? text.downloading : text.download}
          </button>
        </div>
        {truncated && <p className="error-text" role="alert">{text.truncated}</p>}
        {downloadError && <p className="error-text" role="alert">{downloadError}</p>}

        {report.rows.length === 0 ? (
          <p className="empty-state">{text.empty}</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>{text.tableOrder}</th>
                <th>{text.tableProvider}</th>
                <th className="num">{text.tableAmount}</th>
                <th>{text.tableStatus}</th>
                <th>{text.tableReference}</th>
                <th>{text.tableCreated}</th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.order_id}</td>
                  <td>{row.provider}</td>
                  <td className="num">{formatGuaranies(row.amount)} {row.currency}</td>
                  <td>{statusText(row.status)}</td>
                  <td>{row.provider_reference ?? '—'}</td>
                  <td>{isoDateTime(row.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div className="payments-pagination">
          <button type="button" className="btn btn-secondary" disabled={loading || report.offset === 0} onClick={() => setOffset(Math.max(0, report.offset - PAGE_SIZE))}>
            {text.previous}
          </button>
          <span className="muted">
            {text.pageRange.replace('{from}', String(from)).replace('{to}', String(to)).replace('{total}', String(report.total))}
          </span>
          <button type="button" className="btn btn-secondary" disabled={loading || report.offset + report.rows.length >= report.total} onClick={() => setOffset(report.offset + PAGE_SIZE)}>
            {text.next}
          </button>
        </div>
      </section>
    </div>
  )
}
