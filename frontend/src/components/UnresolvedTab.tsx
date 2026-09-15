import { useState } from 'react'
import type { PendingQueue, Resolution, UnresolvedRow } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
import { StatusBadge } from './StatusBadge'

interface UnresolvedTabProps {
  rows: UnresolvedRow[]
  resolutions: Resolution[]
  pending: PendingQueue
  onIgnore: (variantId: string, note: string) => void
}

export function UnresolvedTab({ rows, resolutions, pending, onIgnore }: UnresolvedTabProps) {
  const { t } = useT()
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [helpOpen, setHelpOpen] = useState(false)

  if (rows.length === 0) {
    return (
      <>
        <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.unresolved} />
        {helpOpen && <HelpModal section={t.help.unresolved} onClose={() => setHelpOpen(false)} />}
        <p className="empty-state">{t.unresolved.empty}</p>
      </>
    )
  }

  return (
    <div>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.unresolved} />
      {helpOpen && <HelpModal section={t.help.unresolved} onClose={() => setHelpOpen(false)} />}
      <table className="data-table">
        <thead>
          <tr>
            <th>{t.unresolved.colProduct}</th>
            <th>{t.unresolved.colSku}</th>
            <th>{t.unresolved.colNote}</th>
            <th>{t.unresolved.colStatus}</th>
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
                    placeholder={t.common.optionalNote}
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
                    <span className="badge badge-queued">{t.common.queuedIgnore}</span>
                  ) : (
                    <span className="muted">{t.common.pendingDecision}</span>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={locked}
                    onClick={() => onIgnore(row.variant_id, notes[row.variant_id] ?? '')}
                  >
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
