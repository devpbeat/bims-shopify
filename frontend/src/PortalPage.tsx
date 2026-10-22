import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError, getDashboard, getRekeyReport, getSyncStatus, postRekeyResolutions } from './api/client'
import type { DashboardData, PendingQueue, RekeyReport, Resolution, SyncStatus } from './api/types'
import { ActivityTab } from './components/ActivityTab'
import { ApplyFooter } from './components/ApplyFooter'
import { AutoReconcileBanner } from './components/AutoReconcileBanner'
import { DashboardTab } from './components/DashboardTab'
import { DuplicatesTab } from './components/DuplicatesTab'
import { HelpModal } from './components/HelpModal'
import { LoginCard } from './components/LoginCard'
import { NameMismatchTab } from './components/NameMismatchTab'
import { OperationsTab } from './components/OperationsTab'
import { PaymentsReportTab } from './components/PaymentsReportTab'
import { PaymentsTab } from './components/PaymentsTab'
import { OperatorGate } from './components/OperatorGate'
import { PortalHeader } from './components/PortalHeader'
import { PortalNav } from './components/PortalNav'
import type { PortalNavItem } from './components/PortalNav'
import { TokenGate } from './components/TokenGate'
import { UnresolvedTab } from './components/UnresolvedTab'
import { useT } from './i18n'
import { clearAdminToken, loadAdminToken, saveAdminToken } from './adminSession'
import { clearToken, loadToken, saveToken } from './session'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

type Tab = 'dashboard' | 'duplicates' | 'unresolved' | 'mismatches' | 'activity' | 'payments' | 'paymentsReport' | 'operations'

// These tabs lay out their own cards; the others render inside a shared surface.
const SELF_CONTAINED_TABS: Tab[] = ['dashboard', 'payments', 'paymentsReport', 'operations']

function tokenFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('token')
}

export function PortalPage() {
  const { t } = useT()
  const { slug = '' } = useParams<{ slug: string }>()
  const [token, setToken] = useState<string | null>(() => tokenFromUrl() ?? loadToken(slug))
  const [report, setReport] = useState<RekeyReport | null>(null)
  const [reportMissing, setReportMissing] = useState(false)
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null)
  const [dashboard, setDashboard] = useState<DashboardData | null>(null)
  const [resolutions, setResolutions] = useState<Resolution[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('dashboard')
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

    Promise.all([
      getRekeyReport(slug, token).catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }),
      getSyncStatus(slug, token),
      getDashboard(slug, token),
    ])
      .then(([reportData, syncData, dashboardData]) => {
        if (cancelled) return
        if (reportData === null || reportData.obsolete) {
          setReport(reportData)
          setReportMissing(true)
          setResolutions([])
        } else {
          setReport(reportData)
          setReportMissing(false)
          setResolutions(reportData.resolutions)
        }
        setSyncStatus(syncData)
        setDashboard(dashboardData)
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

  if (loading && !dashboard) {
    return (
      <div className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col gap-4 p-4 sm:p-6" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{t.common.loadingReport}</p>
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }

  if (error && !dashboard) {
    return (
      <div className="mx-auto flex min-h-dvh w-full max-w-md items-center p-4">
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    )
  }

  if (!dashboard) {
    return (
      <div className="mx-auto flex min-h-dvh w-full max-w-md items-center justify-center p-4">
        <p className="text-sm text-muted-foreground">{t.common.noReport}</p>
      </div>
    )
  }

  // Conflict tabs only make sense against a fresh rekey report: once the
  // catalog has been wiped+rebuilt, the old report's findings no longer
  // refer to anything real, so we hide the Duplicates/Unresolved/Name
  // mismatches tabs entirely rather than show stale counts.
  const conflictsVisible = !reportMissing && report != null && !report.obsolete
  const payload = conflictsVisible ? report!.payload : null
  const duplicates = payload?.duplicate_target ?? []
  const unresolved = payload?.unresolved ?? []
  const mismatches = payload?.name_mismatch ?? []

  const guideSection =
    tab === 'unresolved' ? t.help.unresolved : tab === 'mismatches' ? t.help.mismatches : t.help.duplicates

  const navItems: PortalNavItem[] = [
    { value: 'dashboard', label: t.tabs.dashboard },
    ...(conflictsVisible
      ? [
          { value: 'duplicates', label: t.tabs.duplicates, count: duplicates.length },
          { value: 'unresolved', label: t.tabs.unresolved, count: unresolved.length },
          { value: 'mismatches', label: t.tabs.mismatches, count: mismatches.length },
        ]
      : []),
    { value: 'activity', label: t.tabs.activity },
    { value: 'payments', label: t.tabs.payments },
    { value: 'paymentsReport', label: t.tabs.paymentsReport },
    ...(adminToken ? [{ value: 'operations', label: t.operator.tabLabel }] : []),
  ]

  return (
    <div className="min-h-dvh bg-muted/40 pb-24">
      <PortalHeader
        slug={slug}
        syncStatus={syncStatus}
        onLogout={handleLogout}
        isOperator={!!adminToken}
        onOperatorClick={handleOperatorClick}
      />

      {operatorGateOpen && (
        <OperatorGate
          slug={slug}
          error={operatorError}
          onSubmit={handleOperatorSubmit}
          onClose={() => setOperatorGateOpen(false)}
        />
      )}

      <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)} className="gap-0">
        <div className="border-b bg-background">
          <div className="mx-auto w-full max-w-5xl px-4 sm:px-6">
            <PortalNav items={navItems} active={tab} />
          </div>
        </div>

        <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-4 sm:px-6">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <AutoReconcileBanner onViewGuide={() => setGuideOpen(true)} />
          {guideOpen && <HelpModal section={guideSection} onClose={() => setGuideOpen(false)} />}

          <TabsContent
            value={tab}
            className={cn(
              'min-w-0 flex-none',
              !SELF_CONTAINED_TABS.includes(tab) && 'min-h-60 rounded-xl border bg-card p-3 text-card-foreground shadow-sm sm:p-4',
            )}
          >
          {tab === 'dashboard' && (
            <DashboardTab
              slug={slug}
              syncStatus={syncStatus}
              dashboard={dashboard}
              conflictsVisible={conflictsVisible}
              duplicatesCount={duplicates.length}
              unresolvedCount={unresolved.length}
              mismatchesCount={mismatches.length}
              pendingCount={pendingCount}
              adminToken={adminToken}
              onNavigate={(dest) => {
                if (dest === 'operations' && !adminToken) return
                setTab(dest)
              }}
            />
          )}
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
          {tab === 'payments' && <PaymentsTab slug={slug} token={token} />}
          {tab === 'paymentsReport' && <PaymentsReportTab slug={slug} token={token} />}
          {tab === 'operations' && adminToken && (
            <OperationsTab slug={slug} adminToken={adminToken} onUnauthorized={handleOperatorUnauthorized} />
          )}
          </TabsContent>
        </div>
      </Tabs>

      <ApplyFooter pendingCount={pendingCount} applying={applying} onApply={handleApply} />
    </div>
  )
}
