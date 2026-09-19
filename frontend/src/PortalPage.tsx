import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError, getRekeyReport, getSyncStatus, postRekeyResolutions } from './api/client'
import type { PendingQueue, RekeyReport, Resolution, SyncStatus } from './api/types'
import { ActivityTab } from './components/ActivityTab'
import { ApplyFooter } from './components/ApplyFooter'
import { AutoReconcileBanner } from './components/AutoReconcileBanner'
import { DuplicatesTab } from './components/DuplicatesTab'
import { HelpModal } from './components/HelpModal'
import { LoginCard } from './components/LoginCard'
import { NameMismatchTab } from './components/NameMismatchTab'
import { OperationsTab } from './components/OperationsTab'
import { OperatorGate } from './components/OperatorGate'
import { PortalHeader } from './components/PortalHeader'
import { TokenGate } from './components/TokenGate'
import { UnresolvedTab } from './components/UnresolvedTab'
import { useT } from './i18n'
import { clearAdminToken, loadAdminToken, saveAdminToken } from './adminSession'
import { clearToken, loadToken, saveToken } from './session'

type Tab = 'duplicates' | 'unresolved' | 'mismatches' | 'activity' | 'operations'

function tokenFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('token')
}

export function PortalPage() {
  const { t } = useT()
  const { slug = '' } = useParams<{ slug: string }>()
  const [token, setToken] = useState<string | null>(() => tokenFromUrl() ?? loadToken(slug))
  const [report, setReport] = useState<RekeyReport | null>(null)
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null)
  const [resolutions, setResolutions] = useState<Resolution[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('duplicates')
  const [pending, setPending] = useState<PendingQueue>({})
  const [applying, setApplying] = useState(false)
  const [useAccessKey, setUseAccessKey] = useState(false)
  const [guideOpen, setGuideOpen] = useState(false)
  const [adminToken, setAdminToken] = useState<string | null>(() => loadAdminToken(slug))
  const [operatorGateOpen, setOperatorGateOpen] = useState(false)
  const [operatorError, setOperatorError] = useState<string | null>(null)

  function handleLogout() {
    clearToken(slug)
    setToken(null)
    setReport(null)
    setSyncStatus(null)
    setUseAccessKey(false)
  }

  function handleOperatorClick() {
    if (adminToken) {
      clearAdminToken(slug)
      setAdminToken(null)
      if (tab === 'operations') setTab('duplicates')
    } else {
      setOperatorError(null)
      setOperatorGateOpen(true)
    }
  }

  function handleOperatorSubmit(value: string) {
    saveAdminToken(slug, value)
    setAdminToken(value)
    setOperatorError(null)
    setOperatorGateOpen(false)
    setTab('operations')
  }

  function handleOperatorUnauthorized() {
    clearAdminToken(slug)
    setAdminToken(null)
    setOperatorError(t.operator.invalidToken)
    setOperatorGateOpen(true)
    if (tab === 'operations') setTab('duplicates')
  }

  useEffect(() => {
    if (token) saveToken(slug, token)
  }, [slug, token])

  useEffect(() => {
    if (!token) return
    let cancelled = false
    setLoading(true)
    setError(null)

    Promise.all([getRekeyReport(slug, token), getSyncStatus(slug, token)])
      .then(([reportData, syncData]) => {
        if (cancelled) return
        setReport(reportData)
        setResolutions(reportData.resolutions)
        setSyncStatus(syncData)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 401) {
          clearToken(slug)
          setToken(null)
          setError(t.common.sessionExpired)
        } else {
          setError(err instanceof Error ? err.message : t.common.loadFailed)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [slug, token])

  const pendingCount = useMemo(() => Object.keys(pending).length, [pending])

  function queue(variantId: string, action: PendingQueue[string]['action'], note?: string) {
    setPending((prev) => ({ ...prev, [variantId]: { action, note } }))
  }

  function unqueue(variantId: string) {
    setPending((prev) => {
      if (!(variantId in prev)) return prev
      const next = { ...prev }
      delete next[variantId]
      return next
    })
  }

  function handlePickSurvivor(survivorVariantId: string, siblingVariantIds: string[]) {
    queue(survivorVariantId, 'keep')
    for (const siblingId of siblingVariantIds) {
      queue(siblingId, 'delete')
    }
  }

  async function handleApply() {
    if (!token || pendingCount === 0) return
    setApplying(true)
    setError(null)
    try {
      const body = Object.entries(pending).map(([variant_id, entry]) => ({
        variant_id,
        action: entry.action,
        ...(entry.note ? { note: entry.note } : {}),
      }))
      const { results } = await postRekeyResolutions(slug, token, body)
      setResolutions((prev) => {
        const byVariant = new Map(prev.map((resolution) => [resolution.variant_id, resolution]))
        for (const result of results) {
          byVariant.set(result.variant_id, {
            variant_id: result.variant_id,
            action: result.action as Resolution['action'],
            status: result.status,
            error: result.error,
          })
        }
        return [...byVariant.values()]
      })
      for (const result of results) {
        if (result.status !== 'failed') unqueue(result.variant_id)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t.common.applyFailed)
    } finally {
      setApplying(false)
    }
  }

  if (!token) {
    if (useAccessKey) {
      return (
        <TokenGate
          slug={slug}
          onSubmit={(value) => {
            saveToken(slug, value)
            setToken(value)
          }}
          onBackToLogin={() => setUseAccessKey(false)}
        />
      )
    }
    return (
      <LoginCard
        slug={slug}
        onSuccess={(value) => {
          saveToken(slug, value)
          setToken(value)
        }}
        onUseAccessKey={() => setUseAccessKey(true)}
      />
    )
  }

  if (loading && !report) {
    return (
      <div className="centered-page">
        <p>{t.common.loadingReport}</p>
      </div>
    )
  }

  if (error && !report) {
    return (
      <div className="centered-page">
        <p className="error-text">{error}</p>
      </div>
    )
  }

  if (!report) {
    return (
      <div className="centered-page">
        <p className="empty-state">{t.common.noReport}</p>
      </div>
    )
  }

  const payload = report.payload
  const duplicates = payload.duplicate_target ?? []
  const unresolved = payload.unresolved ?? []
  const mismatches = payload.name_mismatch ?? []

  const guideSection =
    tab === 'unresolved' ? t.help.unresolved : tab === 'mismatches' ? t.help.mismatches : t.help.duplicates

  return (
    <div className="portal-page">
      <PortalHeader
        slug={slug}
        reportDate={report.created_at}
        syncStatus={syncStatus}
        onLogout={handleLogout}
        isOperator={!!adminToken}
        onOperatorClick={handleOperatorClick}
      />

      {operatorGateOpen && (
        <div className="operator-gate-overlay">
          <OperatorGate slug={slug} error={operatorError} onSubmit={handleOperatorSubmit} />
        </div>
      )}

      {error && <p className="error-banner">{error}</p>}

      <AutoReconcileBanner onViewGuide={() => setGuideOpen(true)} />
      {guideOpen && <HelpModal section={guideSection} onClose={() => setGuideOpen(false)} />}

      <nav className="tabs">
        <button type="button" className={tab === 'duplicates' ? 'tab active' : 'tab'} onClick={() => setTab('duplicates')}>
          {t.tabs.duplicates} ({duplicates.length})
        </button>
        <button type="button" className={tab === 'unresolved' ? 'tab active' : 'tab'} onClick={() => setTab('unresolved')}>
          {t.tabs.unresolved} ({unresolved.length})
        </button>
        <button type="button" className={tab === 'mismatches' ? 'tab active' : 'tab'} onClick={() => setTab('mismatches')}>
          {t.tabs.mismatches} ({mismatches.length})
        </button>
        <button type="button" className={tab === 'activity' ? 'tab active' : 'tab'} onClick={() => setTab('activity')}>
          {t.tabs.activity}
        </button>
        {adminToken && (
          <button
            type="button"
            className={tab === 'operations' ? 'tab active' : 'tab'}
            onClick={() => setTab('operations')}
          >
            {t.operator.tabLabel}
          </button>
        )}
      </nav>

      <main className="tab-content">
        {tab === 'duplicates' && (
          <DuplicatesTab rows={duplicates} resolutions={resolutions} pending={pending} onPick={handlePickSurvivor} />
        )}
        {tab === 'unresolved' && (
          <UnresolvedTab
            rows={unresolved}
            resolutions={resolutions}
            pending={pending}
            onIgnore={(variantId, note) => queue(variantId, 'ignore', note || undefined)}
          />
        )}
        {tab === 'mismatches' && (
          <NameMismatchTab
            rows={mismatches}
            resolutions={resolutions}
            pending={pending}
            onApprove={(variantId) => queue(variantId, 'approve_sku')}
            onIgnore={(variantId) => queue(variantId, 'ignore')}
          />
        )}
        {tab === 'activity' && <ActivityTab slug={slug} token={token} />}
        {tab === 'operations' && adminToken && (
          <OperationsTab slug={slug} adminToken={adminToken} onUnauthorized={handleOperatorUnauthorized} />
        )}
      </main>

      <ApplyFooter pendingCount={pendingCount} applying={applying} onApply={handleApply} />
    </div>
  )
}
