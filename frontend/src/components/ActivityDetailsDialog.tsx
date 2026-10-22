import { useMemo } from 'react'
import type { AuditEntry } from '../api/types'
import { redactPayload } from '../activity/activityFormat'
import { useT } from '../i18n'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

interface ActivityDetailsDialogProps {
  entry: AuditEntry | null
  actionLabel: string
  absoluteTime: string
  onClose: () => void
}

/** Raw audit record for one event. The payload is redacted before it is rendered. */
export function ActivityDetailsDialog({ entry, actionLabel, absoluteTime, onClose }: ActivityDetailsDialogProps) {
  const { t } = useT()
  const a = t.activity

  const redacted = useMemo(
    () => (entry?.payload ? JSON.stringify(redactPayload(entry.payload), null, 2) : null),
    [entry],
  )

  return (
    <Dialog open={entry !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[85dvh] flex-col gap-4 sm:max-w-xl">
        {entry && (
          <>
            <DialogHeader>
              <DialogTitle>{actionLabel}</DialogTitle>
              <DialogDescription>{a.detailsDescription}</DialogDescription>
            </DialogHeader>

            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted-foreground">{a.timestampLabel}</dt>
              <dd>{absoluteTime}</dd>
              <dt className="text-muted-foreground">{a.originLabel}</dt>
              <dd>
                <Badge variant="outline">{a.origins[entry.actor] ?? entry.actor}</Badge>
              </dd>
              <dt className="text-muted-foreground">{a.actionCodeLabel}</dt>
              <dd className="font-mono text-xs break-all">{entry.action}</dd>
              <dt className="text-muted-foreground">{a.entityLabel}</dt>
              <dd className="break-all">{entry.entity}</dd>
              {entry.entity_id && (
                <>
                  <dt className="text-muted-foreground">{a.entityIdLabel}</dt>
                  <dd className="break-all">{entry.entity_id}</dd>
                </>
              )}
            </dl>

            <section className="flex min-h-0 flex-col gap-2" aria-labelledby="activity-raw-title">
              <h3 id="activity-raw-title" className="text-sm font-medium">
                {a.rawPayloadTitle}
              </h3>
              <p className="text-xs text-muted-foreground">{a.rawPayloadNote}</p>
              {redacted ? (
                <pre
                  tabIndex={0}
                  className="max-h-64 overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                  {redacted}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground">{a.noPayload}</p>
              )}
            </section>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
