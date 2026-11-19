import { describe, expect, it } from 'vitest'
import { en } from './i18n/en'
import { es } from './i18n/es'
import {
  canRunSync,
  canSaveSelection,
  draftFromScope,
  hasUnsavedChanges,
  saveErrorKey,
  syncIsActive,
  toggleWarehouse,
} from './warehouseScopeUtils'

describe('toggleWarehouse', () => {
  it('adds a warehouse keeping the ids sorted and unique', () => {
    expect(toggleWarehouse([27], 24, true)).toEqual([24, 27])
    expect(toggleWarehouse([24, 27], 27, true)).toEqual([24, 27])
  })

  it('removes a warehouse without touching the others', () => {
    expect(toggleWarehouse([24, 26, 27], 26, false)).toEqual([24, 27])
    expect(toggleWarehouse([27], 99, false)).toEqual([27])
  })

  it('never mutates its input', () => {
    const input = [27]
    toggleWarehouse(input, 24, true)
    expect(input).toEqual([27])
  })
})

describe('draftFromScope', () => {
  it('starts from the saved selection, sorted', () => {
    expect(draftFromScope({ selected_ids: [27, 24] })).toEqual([24, 27])
  })
})

describe('hasUnsavedChanges', () => {
  it('compares as sets, ignoring order', () => {
    expect(hasUnsavedChanges([27, 24], [24, 27])).toBe(false)
    expect(hasUnsavedChanges([27], [24, 27])).toBe(true)
    expect(hasUnsavedChanges([], [27])).toBe(true)
  })
})

describe('canSaveSelection', () => {
  it('needs a non-empty draft that differs from the saved one and no save in flight', () => {
    expect(canSaveSelection({ saved: [27], draft: [24, 27], saving: false })).toBe(true)
    expect(canSaveSelection({ saved: [27], draft: [27], saving: false })).toBe(false)
    expect(canSaveSelection({ saved: [27], draft: [], saving: false })).toBe(false)
    expect(canSaveSelection({ saved: [27], draft: [24], saving: true })).toBe(false)
  })
})

describe('canRunSync', () => {
  it('is blocked while a sync runs, while saving, with unsaved changes, or with nothing saved', () => {
    const ready = { saved: [27], draft: [27], saving: false, syncStatus: 'never' as const, starting: false }
    expect(canRunSync(ready)).toBe(true)
    expect(canRunSync({ ...ready, syncStatus: 'running' })).toBe(false)
    expect(canRunSync({ ...ready, starting: true })).toBe(false)
    expect(canRunSync({ ...ready, saving: true })).toBe(false)
    expect(canRunSync({ ...ready, draft: [24, 27] })).toBe(false)
    expect(canRunSync({ ...ready, saved: [], draft: [] })).toBe(false)
  })
})

describe('syncIsActive', () => {
  it('is true only while a sync is running', () => {
    expect(syncIsActive('running')).toBe(true)
    expect(syncIsActive('never')).toBe(false)
    expect(syncIsActive('succeeded')).toBe(false)
    expect(syncIsActive('failed')).toBe(false)
  })
})

describe('saveErrorKey', () => {
  it('maps the API status to a message key', () => {
    expect(saveErrorKey(401)).toBe('sessionExpired')
    expect(saveErrorKey(422)).toBe('unknownIds')
    expect(saveErrorKey(502)).toBe('bimsUnavailable')
    expect(saveErrorKey(500)).toBe('saveFailed')
    expect(saveErrorKey(undefined)).toBe('saveFailed')
  })
})

describe('i18n', () => {
  it('has the same non-empty strings in both languages', () => {
    const keys = Object.keys(en.warehouses).sort()
    expect(Object.keys(es.warehouses).sort()).toEqual(keys)
    for (const key of keys) {
      const english = (en.warehouses as Record<string, unknown>)[key]
      const spanish = (es.warehouses as Record<string, unknown>)[key]
      expect(typeof spanish).toBe(typeof english)
      if (typeof english === 'string') expect(spanish).not.toBe('')
    }
    expect(es.tabs.warehouses).not.toBe('')
  })

  it('warns that zero scoped stock publishes zero, in both languages', () => {
    expect(en.warehouses.warning.toLowerCase()).toContain('zero')
    expect(es.warehouses.warning.toLowerCase()).toContain('cero')
  })

  it('formats the parameterised strings', () => {
    expect(en.warehouses.warehouseLabel(27, 'MAYORISTA')).toBe('MAYORISTA (ID 27)')
    expect(en.warehouses.warehouseLabel(5, null)).toContain('ID 5')
    expect(es.warehouses.selectedCount(1)).toBe('1 seleccionado')
    expect(es.warehouses.selectedCount(2)).toBe('2 seleccionados')
  })
})
