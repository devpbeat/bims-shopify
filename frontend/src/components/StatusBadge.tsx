import { useT } from '../i18n'
import { statusLabel } from '../resolutionUtils'

export function StatusBadge({ status }: { status: string }) {
  const { t } = useT()
  return <span className={`badge badge-${status}`}>{statusLabel(status, t)}</span>
}
