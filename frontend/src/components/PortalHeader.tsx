import type { SyncStatus } from '../api/types'
import { useT } from '../i18n'
import { LanguageToggle } from './LanguageToggle'

interface PortalHeaderProps {
  slug: string
  reportDate: string | null
  syncStatus: SyncStatus | null
  onLogout: () => void
}

export function PortalHeader({ slug, reportDate, syncStatus, onLogout }: PortalHeaderProps) {
  const { t, locale } = useT()

  function formatDate(value: string | null | undefined): string {
    if (!value) return t.common.never
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return date.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')
  }

  return (
    <header className="portal-header">
      <div>
        <h1>{slug}</h1>
        <p className="muted">{t.header.subtitle}</p>
      </div>
      <div className="portal-header-meta">
        <div>
          <span className="label">{t.common.reportDate}</span>
          <span>{formatDate(reportDate)}</span>
        </div>
        <div>
          <span className="label">{t.common.lastSync}</span>
          <span>{formatDate(syncStatus?.last_run as string | undefined)}</span>
        </div>
        <LanguageToggle />
        <button type="button" className="btn btn-secondary" onClick={onLogout}>
          {t.common.logout}
        </button>
      </div>
    </header>
  )
}
