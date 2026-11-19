import { TriangleAlertIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { ApiError, getWarehouseScope, getWarehouseSyncState, saveWarehouseScope, startWarehouseSync } from '../api/client'
import type { WarehouseScope, WarehouseSyncState } from '../api/types'
import { useT } from '../i18n'
import { canRunSync, canSaveSelection, draftFromScope, hasUnsavedChanges, saveErrorKey, syncIsActive, toggleWarehouse } from '../warehouseScopeUtils'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

const POLL_MS = 4000
const touch = 'h-11 sm:h-9'

interface WarehousesTabProps {
  slug: string
  token: string
}

/**
 * Lets the merchant pick which BIMS warehouses feed the store and start a sync.
 * The sync status is polled from the stored sync job with the same tick pattern
 * as the products-without-photos scan: one server-side job row, no second mechanism.
 */
export function WarehousesTab({ slug, token }: WarehousesTabProps) {
  const { t, locale } = useT()
  const text = t.warehouses
  const [scope, setScope] = useState<WarehouseScope | null>(null)
  const [draft, setDraft] = useState<number[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [sync, setSync] = useState<WarehouseSyncState | null>(null)
  const [tick, setTick] = useState(0)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [justSaved, setJustSaved] = useState(false)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    getWarehouseScope(slug, token)
      .then((result) => {
        if (cancelled) return
        setScope(result)
        setDraft(draftFromScope(result))
        setLoadError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoadError(err instanceof ApiError && err.status === 401 ? text.sessionExpired : text.loadFailed)
      })
    return () => {
      cancelled = true
    }
  }, [slug, token, reload, text.loadFailed, text.sessionExpired])

  useEffect(() => {
    let cancelled = false
    getWarehouseSyncState(slug, token)
      .then((state) => {
        if (!cancelled) setSync(state)
      })
      .catch(() => {
        // The status is secondary: keep the last known one and try again on the next tick.
      })
    return () => {
      cancelled = true
    }
  }, [slug, token, tick])

  const syncing = sync ? syncIsActive(sync.status) : false
  useEffect(() => {
    if (!syncing) return
    const id = setTimeout(() => setTick((value) => value + 1), POLL_MS)
    return () => clearTimeout(id)
  }, [syncing, tick])

  async function handleSave() {
    setSaving(true)
    setSaveError(null)
    setJustSaved(false)
    try {
      const saved = await saveWarehouseScope(slug, token, draft)
      setScope(saved)
      setDraft(draftFromScope(saved))
      setJustSaved(true)
    } catch (err) {
      setSaveError(text[saveErrorKey(err instanceof ApiError ? err.status : undefined)])
    } finally {
      setSaving(false)
    }
  }

  async function handleSync() {
    setStarting(true)
    setStartError(null)
    try {
      await startWarehouseSync(slug, token)
    } catch (err) {
      // 409: a sync is already running, which is exactly what the status will show.
      if (!(err instanceof ApiError && err.status === 409)) {
        setStartError(err instanceof ApiError && err.status === 401 ? text.sessionExpired : text.syncStartFailed)
        setStarting(false)
        return
      }
    }
    setStarting(false)
    setTick((value) => value + 1)
  }

  if (!scope) {
    if (loadError) {
      return (
        <div className="flex flex-col gap-3">
          <Alert variant="destructive" role="alert">
            <AlertDescription>{loadError}</AlertDescription>
          </Alert>
          <Button type="button" variant="outline" className={cn(touch, 'w-full sm:w-auto sm:self-start')} onClick={() => setReload((value) => value + 1)}>
            {text.retry}
          </Button>
        </div>
      )
    }
    return (
      <div className="flex flex-col gap-3" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{text.loading}</p>
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const saved = scope.selected_ids
  const unsaved = hasUnsavedChanges(saved, draft)
  const syncAllowed = canRunSync({ saved, draft, saving, syncStatus: sync?.status ?? 'never', starting })
  const syncBusy = syncing || starting
  const finishedAt = sync?.finished_at ? new Date(sync.finished_at).toLocaleString(locale === 'es' ? 'es-419' : 'en-US') : null

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <CardDescription>{text.intro}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Alert role="note">
            <TriangleAlertIcon aria-hidden="true" />
            <AlertDescription>{text.warning}</AlertDescription>
          </Alert>

          {!scope.active && (
            <p className="text-sm text-muted-foreground" role="status">
              {text.inactive}
            </p>
          )}
          {scope.selected_ids_not_found.length > 0 && (
            <Alert role="status">
              <AlertDescription>{text.staleIds(scope.selected_ids_not_found)}</AlertDescription>
            </Alert>
          )}

          <fieldset className="flex flex-col gap-1">
            <legend className="mb-2 text-sm font-medium">{text.selectorLabel}</legend>
            {scope.warehouses.map((warehouse) => {
              const checkboxId = `warehouse-${warehouse.id}`
              return (
                <div key={warehouse.id} className="flex min-h-11 items-center gap-3 rounded-md border px-3 py-2 sm:min-h-9">
                  <Checkbox
                    id={checkboxId}
                    checked={draft.includes(warehouse.id)}
                    disabled={saving}
                    onCheckedChange={(checked) => {
                      setJustSaved(false)
                      setDraft((prev) => toggleWarehouse(prev, warehouse.id, checked === true))
                    }}
                  />
                  <Label htmlFor={checkboxId} className="flex-1 cursor-pointer">
                    {text.warehouseLabel(warehouse.id, warehouse.name)}
                  </Label>
                  {warehouse.synced && <Badge variant="secondary">{text.synced}</Badge>}
                </div>
              )
            })}
          </fieldset>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Button type="button" className={cn(touch, 'w-full sm:w-auto')} disabled={!canSaveSelection({ saved, draft, saving })} onClick={handleSave}>
              {saving ? text.saving : text.save}
            </Button>
            <span className="text-sm text-muted-foreground" aria-live="polite">
              {text.selectedCount(draft.length)}
              {unsaved && ` · ${text.unsavedHint}`}
            </span>
          </div>
          {draft.length === 0 && (
            <p className="text-sm text-muted-foreground" role="status">
              {text.selectAtLeastOne}
            </p>
          )}
          {saveError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{saveError}</AlertDescription>
            </Alert>
          )}
          {justSaved && !saveError && (
            <Alert role="status">
              <AlertDescription>{text.saved}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="flex flex-col gap-3">
          <Button type="button" className={cn(touch, 'w-full sm:w-auto sm:self-start')} disabled={!syncAllowed} onClick={handleSync}>
            {syncBusy ? text.syncing : text.runSync}
          </Button>
          {!syncAllowed && !syncBusy && (saved.length === 0 || unsaved) && (
            <p className="text-sm text-muted-foreground">{text.syncSaveFirst}</p>
          )}
          <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
            {syncing && sync
              ? sync.message || text.syncProgress(sync.progress_done ?? 0, sync.progress_total)
              : sync?.status === 'succeeded'
                ? `${text.syncSucceeded}${finishedAt ? ` (${finishedAt})` : ''}`
                : sync?.status === 'failed'
                  ? text.syncFailed
                  : text.syncNever}
          </p>
          {startError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{startError}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
