import { useT } from '../i18n'
import { Button } from '@/components/ui/button'

interface ApplyFooterProps {
  pendingCount: number
  applying: boolean
  onApply: () => void
}

export function ApplyFooter({ pendingCount, applying, onApply }: ApplyFooterProps) {
  const { t } = useT()
  return (
    <footer className="fixed inset-x-0 bottom-0 z-20 border-t bg-background pb-[env(safe-area-inset-bottom)] shadow-[0_-1px_4px_rgb(0_0_0/0.06)]">
      <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <span className="min-w-0 text-sm text-muted-foreground" aria-live="polite">
          {pendingCount === 0
            ? t.common.noPendingChanges
            : `${pendingCount} ${pendingCount === 1 ? t.common.pendingChange : t.common.pendingChanges}`}
        </span>
        <Button type="button" className="h-11 shrink-0 sm:h-9" disabled={pendingCount === 0 || applying} onClick={onApply}>
          {applying ? t.common.applying : t.common.applyChanges}
        </Button>
      </div>
    </footer>
  )
}
