import { useState } from 'react'
import type { PendingQueue, Resolution, UnresolvedRow } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
import { ResponsiveRecords } from './ResponsiveRecords'
import type { RecordColumn } from './ResponsiveRecords'
import { RowStatus } from './StatusBadge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

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

  const header = (
    <>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.unresolved} />
      {helpOpen && <HelpModal section={t.help.unresolved} onClose={() => setHelpOpen(false)} />}
    </>
  )

  if (rows.length === 0) {
    return (
      <>
        {header}
        <p className="py-6 text-center text-sm text-muted-foreground">{t.unresolved.empty}</p>
      </>
    )
  }

  const columns: RecordColumn<UnresolvedRow>[] = [
    { id: 'product', header: t.unresolved.colProduct, cell: (row) => row.product_title },
    { id: 'sku', header: t.unresolved.colSku, cell: (row) => <code className="font-mono text-xs">{row.sku}</code> },
    {
      id: 'note',
      header: t.unresolved.colNote,
      cell: (row) => (
        <Input
          aria-label={`${t.unresolved.colNote}: ${row.product_title}`}
          className="h-11 sm:h-9"
          placeholder={t.common.optionalNote}
          disabled={isLocked(resolutions, row.variant_id)}
          value={notes[row.variant_id] ?? ''}
          onChange={(event) => setNotes((prev) => ({ ...prev, [row.variant_id]: event.target.value }))}
        />
      ),
    },
    {
      id: 'status',
      header: t.unresolved.colStatus,
      cell: (row) => (
        <RowStatus
          resolution={findResolution(resolutions, row.variant_id)}
          queuedLabel={pending[row.variant_id] ? t.common.queuedIgnore : null}
        />
      ),
    },
  ]

  return (
    <div>
      {header}
      <ResponsiveRecords
        rows={rows}
        columns={columns}
        getKey={(row) => row.variant_id}
        label={t.tabs.unresolved}
        actionsHeader={t.common.actions}
        actions={(row) => (
          <Button
            type="button"
            variant="outline"
            className="h-11 sm:h-9"
            disabled={isLocked(resolutions, row.variant_id)}
            onClick={() => onIgnore(row.variant_id, notes[row.variant_id] ?? '')}
          >
            {t.common.ignore}
          </Button>
        )}
      />
    </div>
  )
}
