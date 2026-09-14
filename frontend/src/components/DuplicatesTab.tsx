import type { DuplicateTargetRow, PendingQueue, Resolution } from '../api/types'
import { findResolution, isLocked } from '../resolutionUtils'
import { StatusBadge } from './StatusBadge'

interface DuplicatesTabProps {
  rows: DuplicateTargetRow[]
  resolutions: Resolution[]
  pending: PendingQueue
  onPick: (survivorVariantId: string, siblingVariantIds: string[]) => void
}

function groupBySku(rows: DuplicateTargetRow[]): Map<string, DuplicateTargetRow[]> {
  const groups = new Map<string, DuplicateTargetRow[]>()
  for (const row of rows) {
    const bucket = groups.get(row.new_sku)
    if (bucket) bucket.push(row)
    else groups.set(row.new_sku, [row])
  }
  return groups
}

export function DuplicatesTab({ rows, resolutions, pending, onPick }: DuplicatesTabProps) {
  if (rows.length === 0) {
    return <p className="empty-state">No duplicate SKU targets in this report.</p>
  }

  const groups = groupBySku(rows)

  return (
    <div className="group-list">
      {[...groups.entries()].map(([newSku, groupRows]) => (
        <div className="group-card" key={newSku}>
          <div className="group-card-header">
            <span className="label">Target SKU</span>
            <code>{newSku}</code>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Keep</th>
                <th>Product</th>
                <th>Old SKU</th>
                <th>BIMS name</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {groupRows.map((row) => {
                const resolution = findResolution(resolutions, row.variant_id)
                const locked = isLocked(resolutions, row.variant_id)
                const pendingEntry = pending[row.variant_id]
                const checked = pendingEntry?.action === 'keep'
                return (
                  <tr key={row.variant_id}>
                    <td>
                      <input
                        type="radio"
                        name={`dup-${newSku}`}
                        checked={checked}
                        disabled={locked}
                        onChange={() =>
                          onPick(
                            row.variant_id,
                            groupRows.map((sibling) => sibling.variant_id).filter((id) => id !== row.variant_id),
                          )
                        }
                      />
                    </td>
                    <td>{row.product_title}</td>
                    <td>
                      <code>{row.old_sku}</code>
                    </td>
                    <td>{row.bims_name}</td>
                    <td>
                      {resolution ? (
                        <StatusBadge status={resolution.status} />
                      ) : pendingEntry ? (
                        <span className="badge badge-queued">
                          Queued: {pendingEntry.action === 'keep' ? 'keep' : 'delete'}
                        </span>
                      ) : (
                        <span className="muted">Pending decision</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}
