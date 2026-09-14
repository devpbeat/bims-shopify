import { statusLabel } from '../resolutionUtils'

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{statusLabel(status)}</span>
}
