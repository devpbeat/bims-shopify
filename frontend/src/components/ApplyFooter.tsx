import { useT } from '../i18n'

interface ApplyFooterProps {
  pendingCount: number
  applying: boolean
  onApply: () => void
}

export function ApplyFooter({ pendingCount, applying, onApply }: ApplyFooterProps) {
  const { t } = useT()
  return (
    <footer className="apply-footer">
      <span>
        {pendingCount === 0
          ? t.common.noPendingChanges
          : `${pendingCount} ${pendingCount === 1 ? t.common.pendingChange : t.common.pendingChanges}`}
      </span>
      <button type="button" className="btn btn-primary" disabled={pendingCount === 0 || applying} onClick={onApply}>
        {applying ? t.common.applying : t.common.applyChanges}
      </button>
    </footer>
  )
}
