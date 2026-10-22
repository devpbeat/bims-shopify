import { useState } from 'react'
import type { NameMismatchRow, PendingQueue, Resolution } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
import { ResponsiveRecords } from './ResponsiveRecords'
import type { RecordColumn } from './ResponsiveRecords'
import { RowStatus } from './StatusBadge'
import { Button } from '@/components/ui/button'

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

  const header = (
    <>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.mismatches} />
      {helpOpen && <HelpModal section={t.help.mismatches} onClose={() => setHelpOpen(false)} />}
    </>
  )

  if (rows.length === 0) {
    return (
      <>
        {header}
        <p className="py-6 text-center text-sm text-muted-foreground">{t.mismatches.empty}</p>
      </>
    )
  }

  const columns: RecordColumn<NameMismatchRow>[] = [
    { id: 'product', header: t.mismatches.colProduct, cell: (row) => row.product_title },
    { id: 'bims', header: t.mismatches.colBimsName, cell: (row) => row.bims_name },
    { id: 'old', header: t.mismatches.colOldSku, cell: (row) => <code className="font-mono text-xs">{row.old_sku}</code> },
    { id: 'new', header: t.mismatches.colNewSku, cell: (row) => <code className="font-mono text-xs">{row.new_sku}</code> },
    {
      id: 'status',
      header: t.mismatches.colStatus,
      cell: (row) => {
        const entry = pending[row.variant_id]
        const queued = entry ? (entry.action === 'approve_sku' ? t.common.queuedApprove : t.common.queuedIgnore) : null
        return <RowStatus resolution={findResolution(resolutions, row.variant_id)} queuedLabel={queued} />
      },
    },
  ]

  return (
    <div>
      {header}
      <ResponsiveRecords
        rows={rows}
        columns={columns}
        getKey={(row) => row.variant_id}
        label={t.tabs.mismatches}
        actionsHeader={t.common.actions}
        actions={(row) => {
          const locked = isLocked(resolutions, row.variant_id)
          return (
            <>
              <Button type="button" className="h-11 sm:h-9" disabled={locked} onClick={() => onApprove(row.variant_id)}>
                {t.common.approve}
              </Button>
              <Button type="button" variant="outline" className="h-11 sm:h-9" disabled={locked} onClick={() => onIgnore(row.variant_id)}>
                {t.common.ignore}
              </Button>
            </>
          )
        }}
      />
    </div>
  )
}
