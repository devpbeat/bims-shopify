import { describe, expect, it } from 'vitest'
import { en } from './i18n/en'
import { es } from './i18n/es'
import { formatStock, photosQueryString, pageBounds, scanIsActive } from './productsWithoutPhotosUtils'

describe('photosQueryString', () => {
  it('sends the scope and the page size, and the page as given', () => {
    expect(photosQueryString({ scope: 'live', q: '', page: 2, pageSize: 50 })).toBe('?scope=live&page=2&page_size=50')
  })

  it('trims the search and leaves it out when blank', () => {
    expect(photosQueryString({ scope: 'all', q: '  ', page: 1, pageSize: 50 })).not.toContain('q=')
    expect(photosQueryString({ scope: 'all', q: ' ab c ', page: 1, pageSize: 50 })).toContain('q=ab+c')
  })
})

describe('formatStock', () => {
  it('shows unknown stock as the placeholder, never as zero', () => {
    expect(formatStock(null, 'en', '?')).toBe('?')
    expect(formatStock(0, 'en', '?')).toBe('0')
  })

  it('formats whole and fractional quantities', () => {
    expect(formatStock(1200, 'en', '?')).toBe('1,200')
    expect(formatStock(2.5, 'en', '?')).toBe('2.5')
  })
})

describe('pageBounds', () => {
  it('reports 0-0 for an empty result', () => {
    expect(pageBounds(1, 50, 0, 0)).toEqual({ from: 0, to: 0 })
  })

  it('reports the range of the current page', () => {
    expect(pageBounds(2, 50, 50, 120)).toEqual({ from: 51, to: 100 })
    expect(pageBounds(3, 50, 20, 120)).toEqual({ from: 101, to: 120 })
  })
})

describe('scanIsActive', () => {
  it('is true only while a scan is running', () => {
    expect(scanIsActive('running')).toBe(true)
    expect(scanIsActive('ready')).toBe(false)
    expect(scanIsActive('never')).toBe(false)
    expect(scanIsActive('failed')).toBe(false)
  })
})

describe('i18n', () => {
  it('has the same non-empty strings in both languages', () => {
    const keys = Object.keys(en.noPhotos).sort()
    expect(Object.keys(es.noPhotos).sort()).toEqual(keys)
    for (const key of keys) {
      const english = (en.noPhotos as Record<string, unknown>)[key]
      const spanish = (es.noPhotos as Record<string, unknown>)[key]
      expect(typeof spanish).toBe(typeof english)
      if (typeof english === 'string') expect(spanish).not.toBe('')
    }
  })
})
