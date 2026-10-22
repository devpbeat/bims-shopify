import { useT } from '../i18n'
import { statusLabel } from '../resolutionUtils'
import type { Resolution } from '../api/types'
import { Badge } from '@/components/ui/badge'

export function StatusBadge({ status }: { status: string }) {
  const { t } = useT()
  const variant = status === 'failed' ? 'destructive' : status === 'already_resolved' ? 'outline' : 'secondary'
  return <Badge variant={variant}>{statusLabel(status, t)}</Badge>
}

interface RowStatusProps {
  resolution: Resolution | undefined
  /** Label of the queued (not yet applied) decision, if any. */
  queuedLabel: string | null
}

/** Applied resolution, queued decision, or "pending decision" for one report row. */
export function RowStatus({ resolution, queuedLabel }: RowStatusProps) {
  const { t } = useT()
  if (resolution) return <StatusBadge status={resolution.status} />
  if (queuedLabel) return <Badge>{queuedLabel}</Badge>
  return <span className="text-muted-foreground">{t.common.pendingDecision}</span>
}
