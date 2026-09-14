import type { NameMismatchRow, PendingQueue, Resolution } from '../api/types'
import { findResolution, isLocked } from '../resolutionUtils'
import { StatusBadge } from './StatusBadge'

interface NameMismatchTabProps {
  rows: NameMismatchRow[]
  resolutions: Resolution[]
  pending: PendingQueue
  onApprove: (variantId: string) => void
  onIgnore: (variantId: string) => void
}

export function NameMismatchTab({ rows, resolutions, pending, onApprove, onIgnore }: NameMismatchTabProps) {
  if (rows.length === 0) {
    return <p className="empty-state">No name mismatches in this report.</p>
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Product</th>
          <th>BIMS name</th>
          <th>Old SKU</th>
          <th>New SKU</th>
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
              <td>{row.bims_name}</td>
              <td>
                <code>{row.old_sku}</code>
              </td>
              <td>
                <code>{row.new_sku}</code>
              </td>
              <td>
                {resolution ? (
                  <StatusBadge status={resolution.status} />
                ) : pendingEntry ? (
                  <span className="badge badge-queued">
                    Queued: {pendingEntry.action === 'approve_sku' ? 'approve' : 'ignore'}
                  </span>
                ) : (
                  <span className="muted">Pending decision</span>
                )}
              </td>
              <td className="row-actions">
                <button type="button" className="btn btn-primary" disabled={locked} onClick={() => onApprove(row.variant_id)}>
                  Approve
                </button>
                <button type="button" className="btn btn-secondary" disabled={locked} onClick={() => onIgnore(row.variant_id)}>
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
