import { useState } from 'react'
import type { NameMismatchRow, PendingQueue, Resolution } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
import { StatusBadge } from './StatusBadge'

interface NameMismatchTabProps {
  rows: NameMismatchRow[]
  resolutions: Resolution[]
  pending: PendingQueue
  onApprove: (variantId: string) => void
  onIgnore: (variantId: string) => void
}

export function NameMismatchTab({ rows, resolutions, pending, onApprove, onIgnore }: NameMismatchTabProps) {
  const { t } = useT()
  const [helpOpen, setHelpOpen] = useState(false)

  if (rows.length === 0) {
    return (
      <>
        <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.mismatches} />
        {helpOpen && <HelpModal section={t.help.mismatches} onClose={() => setHelpOpen(false)} />}
        <p className="empty-state">{t.mismatches.empty}</p>
      </>
    )
  }

  return (
    <div>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.mismatches} />
      {helpOpen && <HelpModal section={t.help.mismatches} onClose={() => setHelpOpen(false)} />}
      <table className="data-table">
        <thead>
          <tr>
            <th>{t.mismatches.colProduct}</th>
            <th>{t.mismatches.colBimsName}</th>
            <th>{t.mismatches.colOldSku}</th>
            <th>{t.mismatches.colNewSku}</th>
            <th>{t.mismatches.colStatus}</th>
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
                      {pendingEntry.action === 'approve_sku' ? t.common.queuedApprove : t.common.queuedIgnore}
                    </span>
                  ) : (
                    <span className="muted">{t.common.pendingDecision}</span>
                  )}
                </td>
                <td className="row-actions">
                  <button type="button" className="btn btn-primary" disabled={locked} onClick={() => onApprove(row.variant_id)}>
                    {t.common.approve}
                  </button>
                  <button type="button" className="btn btn-secondary" disabled={locked} onClick={() => onIgnore(row.variant_id)}>
                    {t.common.ignore}
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
