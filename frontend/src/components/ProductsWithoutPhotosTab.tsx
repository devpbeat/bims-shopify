import { CircleCheckIcon, ExternalLinkIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { ApiError, getProductsWithoutPhotos, startPhotosScan } from '../api/client'
import type { PhotoScope, ProductsWithoutPhotos, ProductWithoutPhotos } from '../api/types'
import { useT } from '../i18n'
import { formatStock, pageBounds, scanIsActive } from '../productsWithoutPhotosUtils'
import { Field } from './Field'
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

const PAGE_SIZE = 25
const POLL_MS = 4000
const SEARCH_DEBOUNCE_MS = 300
const touch = 'h-11 sm:h-9'

interface Loaded {
  key: string
  data?: ProductsWithoutPhotos
  error?: string
}

interface ProductsWithoutPhotosTabProps {
  slug: string
  token: string
  /** Reports the live-without-photos count so the navigation can badge it. */
  onLiveCount: (count: number) => void
}

export function ProductsWithoutPhotosTab({ slug, token, onLiveCount }: ProductsWithoutPhotosTabProps) {
  const { t, locale } = useT()
  const text = t.noPhotos
  const [scope, setScope] = useState<PhotoScope>('live')
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [tick, setTick] = useState(0)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const requestKey = `${scope}|${search}|${page}|${tick}`
  const data = loaded?.data
  const loading = loaded?.key !== requestKey
  const running = data ? scanIsActive(data.scan.status) : false

  useEffect(() => {
    let cancelled = false
    getProductsWithoutPhotos(slug, token, { scope, q: search, page, pageSize: PAGE_SIZE })
      .then((result) => {
        if (!cancelled) setLoaded({ key: requestKey, data: result })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof ApiError && err.status === 401 ? text.sessionExpired : text.loadFailed
        // Keep showing the last good page when only a refresh failed.
        setLoaded((prev) => ({ key: requestKey, data: prev?.data, error: message }))
      })
    return () => {
      cancelled = true
    }
  }, [slug, token, scope, search, page, requestKey, text.loadFailed, text.sessionExpired])

  useEffect(() => {
    if (!running) return
    const id = setTimeout(() => setTick((value) => value + 1), POLL_MS)
    return () => clearTimeout(id)
  }, [running, tick])

  useEffect(() => {
    const id = setTimeout(() => {
      setSearch(searchDraft.trim())
      setPage(1)
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [searchDraft])

  const liveCount = data?.counts?.live
  useEffect(() => {
    if (liveCount !== undefined) onLiveCount(liveCount)
  }, [liveCount, onLiveCount])

  async function handleScan() {
    setStarting(true)
    setStartError(null)
    try {
      await startPhotosScan(slug, token)
    } catch (err) {
      // 409: a scan is already running, which is exactly what we want to show.
      if (!(err instanceof ApiError && err.status === 409)) {
        setStartError(err instanceof ApiError && err.status === 401 ? text.sessionExpired : text.startFailed)
        setStarting(false)
        return
      }
    }
    setStarting(false)
    setTick((value) => value + 1)
  }

  const formatDate = (value: string | null) =>
    value ? new Date(value).toLocaleString(locale === 'es' ? 'es-419' : 'en-US') : null

  if (!loaded) {
    return (
      <div className="flex flex-col gap-3" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{text.loading}</p>
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex flex-col gap-3">
        <Alert variant="destructive" role="alert">
          <AlertDescription>{loaded.error ?? text.loadFailed}</AlertDescription>
        </Alert>
        <Button type="button" variant="outline" className={cn(touch, 'w-full sm:w-auto sm:self-start')} onClick={() => setTick((value) => value + 1)}>
          {text.retry}
        </Button>
      </div>
    )
  }

  const { scan, counts } = data
  const percent = scan.progress_total ? Math.min(100, Math.round(((scan.progress_done ?? 0) / scan.progress_total) * 100)) : null
  const scanning = running || starting
  const scannedAt = formatDate(scan.scanned_at)
  const stockAsOf = formatDate(scan.stock_as_of)

  const statusLabels: Record<string, string> = {
    ACTIVE: text.statusActive,
    DRAFT: text.statusDraft,
    ARCHIVED: text.statusArchived,
  }
  const onStoreText = (value: boolean | null) => (value === null ? text.unknown : value ? text.yes : text.no)

  const columns: RecordColumn<ProductWithoutPhotos>[] = [
    {
      id: 'sku',
      header: text.colSku,
      cardTitle: true,
      cell: (row) => <span className="block max-w-56 font-mono break-all whitespace-normal">{row.skus.length > 0 ? row.skus.join(', ') : text.noSku}</span>,
    },
    { id: 'product', header: text.colProduct, cell: (row) => <span className="block max-w-xs break-words whitespace-normal">{row.title ?? '—'}</span> },
    {
      id: 'status',
      header: text.colStatus,
      cell: (row) => <Badge variant={row.status === 'ACTIVE' ? 'secondary' : 'outline'}>{row.status ? (statusLabels[row.status] ?? row.status) : '—'}</Badge>,
    },
    { id: 'store', header: text.colOnlineStore, cell: (row) => onStoreText(row.on_online_store) },
    { id: 'stock', header: text.colStock, numeric: true, cell: (row) => formatStock(row.stock, locale, text.unknown) },
  ]

  const { from, to } = pageBounds(data.page, data.page_size, data.items.length, data.total)

  function emptyMessage() {
    if (search) return { title: text.emptySearch, hint: null, success: false }
    if (scope === 'all') return { title: text.emptyAll, hint: null, success: true }
    return { title: text.emptyLive, hint: text.emptyLiveHint, success: true }
  }

  const scanControls = (
    <div className="flex flex-col gap-3">
      {running && (
        <div className="flex flex-col gap-2" role="status" aria-live="polite">
          <p className="text-sm">{scan.progress_done ? text.scanProgress(scan.progress_done, scan.progress_total) : text.scanning}</p>
          {percent !== null && (
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted" aria-hidden="true">
              <div className="h-full bg-primary transition-all" style={{ width: `${percent}%` }} />
            </div>
          )}
        </div>
      )}
      {scan.status === 'failed' && (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{text.scanFailed}</AlertDescription>
        </Alert>
      )}
      {startError && (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{startError}</AlertDescription>
        </Alert>
      )}
      <Button
        type="button"
        variant={scan.status === 'never' ? 'default' : 'outline'}
        className={cn(touch, 'w-full sm:w-auto sm:self-start')}
        disabled={scanning}
        onClick={handleScan}
      >
        {scanning ? text.scanning : scan.status === 'never' ? text.scanNow : text.scanAgain}
      </Button>
    </div>
  )

  return (
    <div className="flex flex-col gap-4">
      <Card size="sm">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <CardDescription>{text.intro}</CardDescription>
        </CardHeader>
        <CardContent className="gap-4">
          {counts && (
            <>
              <dl className="grid grid-cols-2 gap-3">
                <div className={cn('flex flex-col-reverse justify-end gap-1 rounded-lg border border-l-4 bg-card p-3', counts.live > 0 ? 'border-l-destructive' : 'border-l-emerald-500')}>
                  <dt className="text-xs text-muted-foreground">{text.liveCount}</dt>
                  <dd className="text-2xl font-semibold tabular-nums">{counts.live.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')}</dd>
                </div>
                <div className="flex flex-col-reverse justify-end gap-1 rounded-lg border border-l-4 border-l-muted-foreground bg-card p-3">
                  <dt className="text-xs text-muted-foreground">{text.allCount}</dt>
                  <dd className="text-2xl font-semibold tabular-nums">{counts.all.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')}</dd>
                </div>
              </dl>
              <p className="text-xs break-words text-muted-foreground">
                {text.scannedCount(counts.scanned)}
                {scannedAt && ` · ${text.lastScan}: ${scannedAt}`}
                {stockAsOf && ` · ${text.stockAsOf}: ${stockAsOf}`}
              </p>
            </>
          )}
          {!counts && scan.status === 'never' && (
            <div className="flex flex-col gap-1 py-2">
              <p className="font-medium">{text.neverScanned}</p>
              <p className="text-sm text-muted-foreground">{text.neverScannedHint}</p>
            </div>
          )}
          {scanControls}
        </CardContent>
      </Card>

      {counts && (
        <Card size="sm" aria-busy={loading}>
          <CardContent className="gap-4">
            {loaded.error && (
              <Alert variant="destructive" role="alert">
                <AlertDescription>{loaded.error}</AlertDescription>
              </Alert>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={text.scope}>
                {(control) => (
                  <Select
                    value={scope}
                    onValueChange={(value) => {
                      setScope(value as PhotoScope)
                      setPage(1)
                    }}
                  >
                    <SelectTrigger {...control} className={cn(touch, 'w-full')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="live">{text.scopeLive}</SelectItem>
                      <SelectItem value="all">{text.scopeAll}</SelectItem>
                    </SelectContent>
                  </Select>
                )}
              </Field>
              <Field label={text.search}>
                {(control) => (
                  <Input
                    {...control}
                    className={touch}
                    type="search"
                    value={searchDraft}
                    placeholder={text.searchPlaceholder}
                    onChange={(event) => setSearchDraft(event.target.value)}
                  />
                )}
              </Field>
            </div>

            {data.truncated && (
              <Alert role="status">
                <AlertDescription>{text.truncated}</AlertDescription>
              </Alert>
            )}

            {data.items.length === 0 ? (
              !loading && (
                <div className="flex flex-col items-center gap-2 py-8 text-center" role="status">
                  {emptyMessage().success && <CircleCheckIcon className="size-8 text-emerald-600" aria-hidden="true" />}
                  <p className="font-medium">{emptyMessage().title}</p>
                  {emptyMessage().hint && <p className="text-sm text-muted-foreground">{emptyMessage().hint}</p>}
                </div>
              )
            ) : (
              <div className={cn(loading && 'opacity-60')}>
                <ResponsiveRecords
                  rows={data.items}
                  columns={columns}
                  getKey={(row) => row.product_id}
                  label={text.listLabel}
                  actionsHeader={text.openInShopify}
                  actions={(row) =>
                    row.admin_url ? (
                      <Button asChild variant="outline" className={cn(touch, 'w-full sm:w-auto')}>
                        <a
                          href={row.admin_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={text.openInShopifyFor(row.title ?? row.skus[0] ?? row.product_id)}
                        >
                          {text.openInShopify}
                          <ExternalLinkIcon aria-hidden="true" />
                        </a>
                      </Button>
                    ) : null
                  }
                />
              </div>
            )}

            {data.total > 0 && (
              <nav aria-label={text.listLabel} className="flex items-center justify-between gap-2">
                <Button type="button" variant="outline" className={touch} disabled={loading || data.page <= 1} onClick={() => setPage(data.page - 1)}>
                  {text.previous}
                </Button>
                <span className="text-center text-xs text-muted-foreground" aria-live="polite">
                  {text.pageRange.replace('{from}', String(from)).replace('{to}', String(to)).replace('{total}', String(data.total))}
                </span>
                <Button type="button" variant="outline" className={touch} disabled={loading || to >= data.total} onClick={() => setPage(data.page + 1)}>
                  {text.next}
                </Button>
              </nav>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
