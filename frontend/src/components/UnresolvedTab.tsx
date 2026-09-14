import { useState } from 'react'
import type { PendingQueue, Resolution, UnresolvedRow } from '../api/types'
import { findResolution, isLocked } from '../resolutionUtils'
import { StatusBadge } from './StatusBadge'

interface UnresolvedTabProps {
  rows: UnresolvedRow[]
  resolutions: Resolution[]
  pending: PendingQueue
  onIgnore: (variantId: string, note: string) => void
}

export function UnresolvedTab({ rows, resolutions, pending, onIgnore }: UnresolvedTabProps) {
  const [notes, setNotes] = useState<Record<string, string>>({})

  if (rows.length === 0) {
    return <p className="empty-state">No unresolved variants in this report.</p>
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Product</th>
          <th>SKU</th>
          <th>Note</th>
          <th>Status</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const resolution = findResolution(resolutions, row.variant_id)
          const locked = isLocked(resolutions, row.variant_id)
          const pendingEntry = pending[row.variant_id]
          return (
            <tr key={row.variant_id}>
              <td>{row.product_title}</td>
              <td>
                <code>{row.sku}</code>
              </td>
              <td>
                <input
                  type="text"
                  placeholder="Optional note"
                  disabled={locked}
                  value={notes[row.variant_id] ?? ''}
                  onChange={(event) =>
                    setNotes((prev) => ({ ...prev, [row.variant_id]: event.target.value }))
                  }
                />
              </td>
              <td>
                {resolution ? (
                  <StatusBadge status={resolution.status} />
                ) : pendingEntry ? (
                  <span className="badge badge-queued">Queued: ignore</span>
                ) : (
                  <span className="muted">Pending decision</span>
                )}
              </td>
              <td>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={locked}
                  onClick={() => onIgnore(row.variant_id, notes[row.variant_id] ?? '')}
                >
                  Ignore
                </button>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
