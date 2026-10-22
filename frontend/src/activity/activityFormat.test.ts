import { describe, expect, it } from 'vitest'
import type { AuditEntry } from '../api/types'
import { en } from '../i18n/en'
import { es } from '../i18n/es'
import {
  ACTIVITY_ACTIONS,
  describeEntry,
  formatRelativeTime,
  formatMetricValue,
  redactPayload,
  REDACTED,
} from './activityFormat'

function entry(action: string, payload: AuditEntry['payload']): AuditEntry {
  return { id: 1, actor: 'system', action, entity: 'catalog', entity_id: null, payload, created_at: '2026-10-22T10:00:00Z' }
}

describe('describeEntry', () => {
  it('surfaces the headline reconciliation counts instead of the raw payload', () => {
    const summary = describeEntry(
      entry('catalog.reconciliation', {
        bims: { eligible_products: 4602, eligible_variants: 11484, duplicate_code2: 3, with_stock: 900 },
        shopify: { products: 4500, variants: 11000, variants_with_sku: 10990 },
        reconciliation: { skus_in_bims_not_in_shopify: 120, skus_in_shopify_not_in_bims: 4, matched_skus: 10880 },
      }),
    )
    expect(summary.known).toBe(true)
    expect(summary.metrics.map((m) => [m.id, m.value])).toEqual([
      ['eligibleProducts', 4602],
      ['shopifyProducts', 4500],
      ['missingInShopify', 120],
    ])
  })

  it('reads nested drift counts and array lengths', () => {
    const drift = describeEntry(entry('sync.reconcile_completed', { drift: { count: 7 }, updated: 5, scanned: { snapshot: 10, shopify: 12 } }))
    expect(drift.metrics.map((m) => m.id)).toEqual(['drift', 'updated'])
    const fields = describeEntry(entry('payments.config_updated', { fields: ['public_key', 'environment'] }))
    expect(fields.metrics).toEqual([{ id: 'fieldsChanged', value: 2, kind: 'number' }])
  })

  it('degrades gracefully for unknown actions and missing payloads', () => {
    const unknown = describeEntry(entry('brand.new_thing', { whatever: 1, nested: { a: 1 } }))
    expect(unknown).toEqual({ known: false, metrics: [] })
    expect(describeEntry(entry('catalog.wipe', null)).metrics).toEqual([])
  })

  it('never leaks secrets through summary metrics', () => {
    const summary = describeEntry(entry('sync.create_error', { error: 'failed at https://x.test/pay/checkout/abc?token=SECRET123 Bearer abc.def' }))
    const text = String(summary.metrics[0].value)
    expect(text).not.toContain('SECRET123')
    expect(text).not.toContain('abc.def')
  })
})

describe('redactPayload', () => {
  it('redacts checkout_url and credential-like keys at any depth', () => {
    const out = redactPayload({
      checkout_url: 'https://pay.example/checkout/123',
      nested: { private_key: 'pk', Authorization: 'Bearer x', api_token: 't', list: [{ password: 'p', ok: 1 }] },
      keep: 'visible',
    }) as Record<string, unknown>
    const serialized = JSON.stringify(out)
    expect(out.checkout_url).toBe(REDACTED)
    expect(serialized).not.toContain('pay.example')
    expect(serialized).not.toContain('"pk"')
    expect(serialized).not.toContain('Bearer x')
    expect(serialized).toContain('visible')
    expect(serialized).toContain('"ok":1')
  })

  it('scrubs secrets embedded in string values and does not mutate the input', () => {
    const input = { error: 'GET https://a.test/checkout/9 failed, secret=abc123' }
    const out = redactPayload(input) as { error: string }
    expect(out.error).not.toContain('/checkout/9')
    expect(out.error).not.toContain('abc123')
    expect(input.error).toContain('abc123')
  })

  it('handles null and primitives', () => {
    expect(redactPayload(null)).toBeNull()
    expect(redactPayload(5)).toBe(5)
  })
})

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-10-22T12:00:00Z')
  it('formats minutes, hours and days in both locales', () => {
    expect(formatRelativeTime('2026-10-22T11:55:00Z', 'en', now)).toBe('5 minutes ago')
    expect(formatRelativeTime('2026-10-22T09:00:00Z', 'en', now)).toBe('3 hours ago')
    expect(formatRelativeTime('2026-10-19T12:00:00Z', 'es', now)).toBe('hace 3 días')
  })
  it('returns null for invalid dates', () => {
    expect(formatRelativeTime('nope', 'en', now)).toBeNull()
  })
})

describe('formatMetricValue', () => {
  it('formats numbers, seconds and booleans', () => {
    expect(formatMetricValue({ id: 'rowsPulled', value: 4602, kind: 'number' }, 'en', { yes: 'Yes', no: 'No' })).toBe('4,602')
    expect(formatMetricValue({ id: 'duration', value: 12.345, kind: 'seconds' }, 'en', { yes: 'Yes', no: 'No' })).toBe('12.3 s')
    expect(formatMetricValue({ id: 'configOk', value: true, kind: 'bool' }, 'es', { yes: 'Sí', no: 'No' })).toBe('Sí')
  })
})

describe('i18n coverage', () => {
  it('has a label for every known action and metric in both locales', () => {
    for (const catalog of [en, es]) {
      for (const action of Object.keys(ACTIVITY_ACTIONS)) {
        expect(catalog.activity.actions[action], action).toBeTruthy()
      }
      for (const spec of Object.values(ACTIVITY_ACTIONS).flat()) {
        expect(catalog.activity.metrics[spec.id], spec.id).toBeTruthy()
      }
    }
  })
})
