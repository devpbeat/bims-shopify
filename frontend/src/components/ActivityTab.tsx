import { useEffect, useState } from 'react'
import { getAuditLog } from '../api/client'
import type { AuditEntry } from '../api/types'
import { useT } from '../i18n'

const PAGE_SIZE = 25

function formatDateTime(value: string, locale: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')
}

function summarizeDetail(entry: AuditEntry): string {
  if (entry.entity_id) {
    return entry.payload ? `${entry.entity_id} · ${JSON.stringify(entry.payload)}` : entry.entity_id
  }
  return entry.payload ? JSON.stringify(entry.payload) : '—'
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

  if (loading && entries.length === 0) {
    return <p className="muted">{t.common.loadingActivity}</p>
  }

  if (error) {
    return <p className="error-text">{error}</p>
  }

  if (entries.length === 0) {
    return <p className="empty-state">{t.common.noActivity}</p>
  }

  return (
    <div className="activity-tab">
      <table className="data-table">
        <thead>
          <tr>
            <th>{t.activity.colTime}</th>
            <th>{t.activity.colActor}</th>
            <th>{t.activity.colAction}</th>
            <th>{t.activity.colDetail}</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td>{formatDateTime(entry.created_at, locale)}</td>
              <td>
                <span className={`badge badge-actor-${entry.actor}`}>{entry.actor}</span>
              </td>
              <td>{entry.action}</td>
              <td className="muted">{summarizeDetail(entry)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {entries.length >= limit && (
        <button
          type="button"
          className="load-more"
          onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
          disabled={loading}
        >
          {loading ? t.common.loading : t.common.loadMore}
        </button>
      )}
    </div>
  )
}
