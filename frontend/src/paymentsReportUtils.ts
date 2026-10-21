import type { PaymentDayAggregate } from './api/types'

/** Guaraníes have no minor unit: whole numbers with a thousands separator. */
export function formatGuaranies(amount: number): string {
  return Math.round(amount).toLocaleString('de-DE', { maximumFractionDigits: 0 })
}

/** ISO date (YYYY-MM-DD) of a timestamp, in UTC; falls back to the raw value. */
export function isoDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toISOString().slice(0, 10)
}

export function isoDateTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.toISOString().slice(0, 10)} ${date.toISOString().slice(11, 16)}`
}

const DAY_MS = 86_400_000

/** One entry per calendar day in [from, to], zero-filled, so gaps read as gaps. */
export function fillDays(
  byDay: PaymentDayAggregate[],
  from: string | null,
  to: string | null,
): PaymentDayAggregate[] {
  const lookup = new Map(byDay.map((d) => [d.date, d]))
  const start = Date.parse(from ?? byDay[0]?.date ?? '')
  const end = Date.parse(to ?? byDay[byDay.length - 1]?.date ?? '')
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return byDay
  const out: PaymentDayAggregate[] = []
  for (let ts = start; ts <= end; ts += DAY_MS) {
    const date = new Date(ts).toISOString().slice(0, 10)
    out.push(lookup.get(date) ?? { date, count: 0, total_amount: 0 })
  }
  return out
}

/** Round a maximum up to a "nice" axis ceiling (1, 2, 5 x 10^n). */
export function niceCeiling(max: number): number {
  if (max <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(max))
  const normalized = max / magnitude
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return step * magnitude
}

export const PAYMENT_STATUSES = ['pending', 'paid', 'failed', 'cancelled'] as const
export const PAYMENT_PROVIDERS = ['bancard_vpos', 'pagopar'] as const
