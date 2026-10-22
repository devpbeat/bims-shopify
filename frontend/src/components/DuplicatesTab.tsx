import { useState } from 'react'
import type { DuplicateTargetRow, PendingQueue, Resolution } from '../api/types'
import { useT } from '../i18n'
import { findResolution, isLocked } from '../resolutionUtils'
import { HelpModal, TabHelpHeader } from './HelpModal'
import { RowStatus } from './StatusBadge'
import { cn } from '@/lib/utils'

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

  const header = (
    <>
      <TabHelpHeader onHelp={() => setHelpOpen(true)} label={t.tabs.duplicates} />
      {helpOpen && <HelpModal section={t.help.duplicates} onClose={() => setHelpOpen(false)} />}
    </>
  )

  if (rows.length === 0) {
    return (
      <>
        {header}
        <p className="py-6 text-center text-sm text-muted-foreground">{t.duplicates.empty}</p>
      </>
    )
  }

  return (
    <div>
      {header}
      <div className="flex flex-col gap-4">
        {[...groupBySku(rows).entries()].map(([newSku, groupRows]) => (
          <fieldset key={newSku} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
            <legend className="px-1 text-xs text-muted-foreground">
              {t.duplicates.targetSku}: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground">{newSku}</code>
            </legend>
            {groupRows.map((row) => {
              const resolution = findResolution(resolutions, row.variant_id)
              const locked = isLocked(resolutions, row.variant_id)
              const pendingEntry = pending[row.variant_id]
              const checked = pendingEntry?.action === 'keep'
              const queuedLabel = pendingEntry ? (pendingEntry.action === 'keep' ? t.common.queuedKeep : t.common.queuedDelete) : null
              return (
                <label
                  key={row.variant_id}
                  className={cn(
                    'flex min-h-11 cursor-pointer items-start gap-3 rounded-md border p-3 has-focus-visible:ring-[3px] has-focus-visible:ring-ring/50',
                    checked && 'border-primary bg-muted/50',
                    locked && 'cursor-not-allowed opacity-70',
                  )}
                >
                  <input
                    type="radio"
                    className="mt-0.5 size-5 shrink-0 accent-primary"
                    name={`dup-${newSku}`}
                    checked={checked}
                    disabled={locked}
                    aria-label={`${t.duplicates.colKeep}: ${row.product_title}`}
                    onChange={() =>
                      onPick(
                        row.variant_id,
                        groupRows.map((sibling) => sibling.variant_id).filter((id) => id !== row.variant_id),
                      )
                    }
                  />
                  <span className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
                    <span className="font-medium break-words">{row.product_title}</span>
                    <span className="break-words text-muted-foreground">{row.bims_name}</span>
                    <span className="text-xs text-muted-foreground">
                      {t.duplicates.colOldSku}: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground">{row.old_sku}</code>
                    </span>
                    <span className="mt-1">
                      <RowStatus resolution={resolution} queuedLabel={queuedLabel} />
                    </span>
                  </span>
                </label>
              )
            })}
          </fieldset>
        ))}
      </div>
    </div>
  )
}
