import { useState } from 'react'
import type { DuplicateTargetRow, PendingQueue, Resolution } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
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
  const { t } = useT()
  const [helpOpen, setHelpOpen] = useState(false)

  if (rows.length === 0) {
    return (
      <>
        <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.duplicates} />
        {helpOpen && <HelpModal section={t.help.duplicates} onClose={() => setHelpOpen(false)} />}
        <p className="empty-state">{t.duplicates.empty}</p>
      </>
    )
  }

  const groups = groupBySku(rows)

  return (
    <div>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.duplicates} />
      {helpOpen && <HelpModal section={t.help.duplicates} onClose={() => setHelpOpen(false)} />}
      <div className="group-list">
        {[...groups.entries()].map(([newSku, groupRows]) => (
          <div className="group-card" key={newSku}>
            <div className="group-card-header">
              <span className="label">{t.duplicates.targetSku}</span>
              <code>{newSku}</code>
            </div>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t.duplicates.colKeep}</th>
                  <th>{t.duplicates.colProduct}</th>
                  <th>{t.duplicates.colOldSku}</th>
                  <th>{t.duplicates.colBimsName}</th>
                  <th>{t.duplicates.colStatus}</th>
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
                            {pendingEntry.action === 'keep' ? t.common.queuedKeep : t.common.queuedDelete}
                          </span>
                        ) : (
                          <span className="muted">{t.common.pendingDecision}</span>
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
    </div>
  )
}
