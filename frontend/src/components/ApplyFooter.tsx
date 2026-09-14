interface ApplyFooterProps {
  pendingCount: number
  applying: boolean
  onApply: () => void
}

export function ApplyFooter({ pendingCount, applying, onApply }: ApplyFooterProps) {
  return (
    <footer className="apply-footer">
      <span>
        {pendingCount === 0 ? 'No pending changes' : `${pendingCount} pending change${pendingCount === 1 ? '' : 's'}`}
      </span>
      <button type="button" className="btn btn-primary" disabled={pendingCount === 0 || applying} onClick={onApply}>
        {applying ? 'Applying…' : 'Apply changes'}
      </button>
    </footer>
  )
}
