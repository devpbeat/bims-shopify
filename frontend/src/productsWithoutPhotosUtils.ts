import type { PhotoScanStatus, ProductsWithoutPhotosQuery } from './api/types'

/** Query string for the products-without-photos list; the search is left out when blank. */
export function photosQueryString(query: ProductsWithoutPhotosQuery): string {
  const params = new URLSearchParams()
  params.set('scope', query.scope)
  const search = query.q.trim()
  if (search) params.set('q', search)
  params.set('page', String(query.page))
  params.set('page_size', String(query.pageSize))
  return `?${params.toString()}`
}

/** A stock figure, or the placeholder when BIMS reported none (unknown is not zero). */
export function formatStock(stock: number | null, locale: 'es' | 'en', unknown: string): string {
  if (stock === null) return unknown
  return stock.toLocaleString(locale === 'es' ? 'es-419' : 'en-US', { maximumFractionDigits: 2 })
}

/** 1-based first/last row numbers of the current page. */
export function pageBounds(page: number, pageSize: number, shown: number, total: number): { from: number; to: number } {
  if (total === 0 || shown === 0) return { from: 0, to: 0 }
  const from = (page - 1) * pageSize + 1
  return { from, to: Math.min(from + shown - 1, total) }
}

export function scanIsActive(status: PhotoScanStatus): boolean {
  return status === 'running'
}
