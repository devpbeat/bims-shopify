import type { AuditEntry } from '../api/types'

export const REDACTED = '[redacted]'

export type MetricKind = 'number' | 'seconds' | 'bool' | 'text'

export type MetricId =
  | 'eligibleProducts'
  | 'shopifyProducts'
  | 'missingInShopify'
  | 'rowsPulled'
  | 'stockRows'
  | 'duration'
  | 'reason'
  | 'detail'
  | 'error'
  | 'updated'
  | 'wouldUpdate'
  | 'drift'
  | 'zeroCount'
  | 'matchedCount'
  | 'threshold'
  | 'command'
  | 'status'
  | 'products'
  | 'variants'
  | 'deleted'
  | 'created'
  | 'failed'
  | 'checked'
  | 'fixed'
  | 'duplicatesFound'
  | 'targets'
  | 'processed'
  | 'totalVariants'
  | 'plannedRewrites'
  | 'duplicateTargets'
  | 'unresolved'
  | 'resolutionAction'
  | 'provider'
  | 'amount'
  | 'currency'
  | 'previousStatus'
  | 'attempt'
  | 'intentsDeleted'
  | 'eventsDeleted'
  | 'configOk'
  | 'topic'
  | 'fieldsChanged'
  | 'outcome'
  | 'slug'

interface MetricSpec {
  id: MetricId
  /** Dot-separated path inside the audit payload. */
  path: string
  kind: MetricKind
}

const n = (id: MetricId, path: string): MetricSpec => ({ id, path, kind: 'number' })
const s = (id: MetricId, path: string): MetricSpec => ({ id, path, kind: 'text' })

/**
 * The 2-3 metrics worth reading for each known action type, chosen from the
 * payloads the backend actually writes. Actions absent from this table are
 * "unknown" and render without a summary (never as raw JSON).
 */
export const ACTIVITY_ACTIONS: Record<string, MetricSpec[]> = {
  'catalog.reconciliation': [
    n('eligibleProducts', 'bims.eligible_products'),
    n('shopifyProducts', 'shopify.products'),
    n('missingInShopify', 'reconciliation.skus_in_bims_not_in_shopify'),
  ],
  'catalog.import': [n('products', 'products'), n('variants', 'variants'), n('created', 'apply.created')],
  'catalog.wipe': [n('deleted', 'deleted'), n('failed', 'failed')],
  'catalog.dedupe': [n('duplicatesFound', 'duplicates_found'), n('deleted', 'deleted'), n('failed', 'failed')],
  'ops.fix_tracking': [n('checked', 'checked'), n('fixed', 'fixed'), n('failed', 'failed')],
  'ops.cleanup_no_stock': [n('targets', 'targets_found'), n('processed', 'processed'), n('failed', 'failed')],
  'ops.publish': [n('targets', 'targets_found'), n('processed', 'processed'), n('failed', 'failed')],
  'ops.job_started': [s('command', 'command')],
  'ops.job_finished': [s('command', 'command'), s('status', 'status'), n('products', 'products')],
  'snapshot.pull_completed': [
    n('rowsPulled', 'rows'),
    n('stockRows', 'stock_rows'),
    { id: 'duration', path: 'duration_seconds', kind: 'seconds' },
  ],
  'snapshot.pull_failed': [s('error', 'error')],
  'snapshot.pull_skipped': [s('reason', 'reason')],
  'snapshot.pull_rejected': [s('reason', 'reason'), s('detail', 'detail')],
  'sync.reconcile_completed': [n('drift', 'drift.count'), n('updated', 'updated')],
  'sync.dry_run': [n('wouldUpdate', 'would_update'), n('drift', 'drift.count')],
  'sync.reconcile_needs_confirmation': [
    n('zeroCount', 'zero_count'),
    n('matchedCount', 'matched_count'),
    n('threshold', 'threshold'),
  ],
  'sync.reconcile_declined': [s('reason', 'reason'), s('detail', 'detail')],
  'sync.reconcile_error': [s('error', 'error')],
  'sync.create_error': [s('error', 'error')],
  'sync.publish_error': [s('error', 'error')],
  'sync.publish_completed': [],
  'rekey.scan_completed': [
    n('totalVariants', 'total_variants'),
    n('duplicateTargets', 'duplicate_target'),
    n('unresolved', 'unresolved'),
  ],
  'rekey.resolution': [s('resolutionAction', 'action'), s('status', 'status')],
  'payment.link_created': [s('provider', 'provider'), s('status', 'status'), n('amount', 'amount')],
  'payment.confirmed': [s('status', 'status')],
  'payment.verification_failed': [s('reason', 'reason'), s('error', 'error')],
  'payment.callback_received': [],
  'payment.retry_minted': [n('attempt', 'attempt'), s('previousStatus', 'previous_status')],
  'payment.metafields_failed': [s('error', 'error')],
  'payment.rollback_requested': [s('outcome', 'outcome')],
  'payment.page_error': [s('reason', 'reason')],
  'payments.purged': [n('intentsDeleted', 'intents_deleted'), n('eventsDeleted', 'events_deleted'), s('provider', 'provider')],
  'payments.config_updated': [n('fieldsChanged', 'fields')],
  'payments.config_tested': [{ id: 'configOk', path: 'ok', kind: 'bool' }, s('reason', 'reason')],
  'portal.login_success': [],
  'portal.login_failed': [],
  'portal.sso_login': [s('outcome', 'outcome')],
  'portal.sso_failed': [s('reason', 'reason')],
  'webhook.order_received': [s('topic', 'topic')],
  'tenant.create': [s('slug', 'slug')],
  'tenant.update': [n('fieldsChanged', 'fields')],
  'tenant.delete': [s('slug', 'slug')],
}

export interface ActivityMetric {
  id: MetricId
  value: string | number | boolean
  kind: MetricKind
}

export interface ActivitySummary {
  known: boolean
  metrics: ActivityMetric[]
}

const MAX_TEXT = 90

/** Keys whose values are never shown, whatever their content. */
const SENSITIVE_KEY = /(token|secret|password|passwd|private_?key|api_?key|authorization|credential|signature|cookie|checkout_?url|session)/i

/** Strip URLs to checkout pages, bearer tokens and key=value credentials from free text. */
function scrubString(value: string): string {
  return value
    .replace(/https?:\/\/[^\s"']*checkout[^\s"']*/gi, REDACTED)
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, `Bearer ${REDACTED}`)
    .replace(/\b(token|secret|password|api_?key|signature|sig|key)=[^\s&"']+/gi, `$1=${REDACTED}`)
}

const MAX_DEPTH = 8
const MAX_ITEMS = 100

/**
 * Deep copy of a payload that is safe to show in the raw view: sensitive keys
 * (including `checkout_url`) are replaced and credential-looking text inside
 * strings is scrubbed. The input is never mutated.
 */
export function redactPayload(value: unknown, depth = 0): unknown {
  if (value === null || value === undefined) return value ?? null
  if (typeof value === 'string') return scrubString(value)
  if (typeof value !== 'object') return value
  if (depth >= MAX_DEPTH) return REDACTED
  if (Array.isArray(value)) return value.slice(0, MAX_ITEMS).map((item) => redactPayload(item, depth + 1))
  const out: Record<string, unknown> = {}
  for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
    out[key] = SENSITIVE_KEY.test(key) ? REDACTED : redactPayload(inner, depth + 1)
  }
  return out
}

function readPath(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload
  for (const part of path.split('.')) {
    if (current === null || typeof current !== 'object' || Array.isArray(current)) return undefined
    current = (current as Record<string, unknown>)[part]
  }
  return current
}

function toMetric(spec: MetricSpec, raw: unknown): ActivityMetric | null {
  if (Array.isArray(raw)) return { id: spec.id, value: raw.length, kind: 'number' }
  if (typeof raw === 'number' && Number.isFinite(raw)) return { id: spec.id, value: raw, kind: spec.kind }
  if (typeof raw === 'boolean') return { id: spec.id, value: raw, kind: 'bool' }
  if (typeof raw === 'string' && raw.trim() !== '') {
    const text = scrubString(raw.trim())
    return { id: spec.id, value: text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT - 1)}…` : text, kind: 'text' }
  }
  return null
}

export function describeEntry(entry: Pick<AuditEntry, 'action' | 'payload'>): ActivitySummary {
  const specs = ACTIVITY_ACTIONS[entry.action]
  if (!specs) return { known: false, metrics: [] }
  const payload = entry.payload
  if (!payload || typeof payload !== 'object') return { known: true, metrics: [] }
  const metrics: ActivityMetric[] = []
  for (const spec of specs) {
    const metric = toMetric(spec, readPath(payload, spec.path))
    if (metric) metrics.push(metric)
  }
  return { known: true, metrics }
}

export function formatMetricValue(
  metric: ActivityMetric,
  locale: 'es' | 'en',
  words: { yes: string; no: string },
): string {
  const tag = locale === 'es' ? 'es-419' : 'en-US'
  if (typeof metric.value === 'boolean') return metric.value ? words.yes : words.no
  if (typeof metric.value === 'number') {
    if (metric.kind === 'seconds') {
      return `${metric.value.toLocaleString(tag, { maximumFractionDigits: 1, minimumFractionDigits: 1 })} s`
    }
    return metric.value.toLocaleString(tag)
  }
  return metric.value
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['day', 86_400_000],
  ['hour', 3_600_000],
  ['minute', 60_000],
]

/** "5 minutes ago" / "hace 2 días"; null when the timestamp is not a valid date. */
export function formatRelativeTime(value: string, locale: 'es' | 'en', now: number = Date.now()): string | null {
  const time = Date.parse(value)
  if (Number.isNaN(time)) return null
  const formatter = new Intl.RelativeTimeFormat(locale === 'es' ? 'es' : 'en', { numeric: 'auto' })
  const diff = time - now
  for (const [unit, ms] of UNITS) {
    if (Math.abs(diff) >= ms) return formatter.format(Math.round(diff / ms), unit)
  }
  return formatter.format(0, 'minute')
}
