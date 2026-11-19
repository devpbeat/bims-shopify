import type { WarehouseScope, WarehouseSyncStatus } from './api/types'
import type { Messages } from './i18n/types'

/** Check or uncheck one warehouse; the result is sorted, duplicate-free and a new array. */
export function toggleWarehouse(selected: number[], id: number, checked: boolean): number[] {
  const rest = selected.filter((value) => value !== id)
  return (checked ? [...rest, id] : rest).sort((a, b) => a - b)
}

export function draftFromScope(scope: Pick<WarehouseScope, 'selected_ids'>): number[] {
  return [...scope.selected_ids].sort((a, b) => a - b)
}

/** True when the draft and the saved selection differ as sets. */
export function hasUnsavedChanges(saved: number[], draft: number[]): boolean {
  if (saved.length !== draft.length) return true
  const savedIds = new Set(saved)
  return draft.some((id) => !savedIds.has(id))
}

export function canSaveSelection({ saved, draft, saving }: { saved: number[]; draft: number[]; saving: boolean }): boolean {
  return !saving && draft.length > 0 && hasUnsavedChanges(saved, draft)
}

/** A sync only makes sense for a saved selection: it reads what is stored, not the draft. */
export function canRunSync(state: {
  saved: number[]
  draft: number[]
  saving: boolean
  syncStatus: WarehouseSyncStatus
  starting: boolean
}): boolean {
  return (
    state.saved.length > 0 &&
    !hasUnsavedChanges(state.saved, state.draft) &&
    !state.saving &&
    !state.starting &&
    !syncIsActive(state.syncStatus)
  )
}

export function syncIsActive(status: WarehouseSyncStatus): boolean {
  return status === 'running'
}

type SaveErrorKey = keyof Pick<Messages['warehouses'], 'sessionExpired' | 'unknownIds' | 'bimsUnavailable' | 'saveFailed'>

/** Which translated message describes a failed save, from the API status. */
export function saveErrorKey(status: number | undefined): SaveErrorKey {
  if (status === 401) return 'sessionExpired'
  if (status === 422) return 'unknownIds'
  if (status === 502) return 'bimsUnavailable'
  return 'saveFailed'
}
