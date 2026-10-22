import { useT } from '../i18n'
import type { HelpSection } from '../i18n/types'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface HelpModalProps {
  section: HelpSection
  onClose: () => void
}

export function HelpModal({ section, onClose }: HelpModalProps) {
  const { t } = useT()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {section.title} {t.help.modalTitleSuffix}
          </DialogTitle>
          <DialogDescription className="sr-only">{section.title}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 text-sm leading-relaxed">
          <p>{section.what}</p>
          <p>{section.howAutoResolves}</p>
          <p>{section.manualActions}</p>
          <div className="rounded-lg border bg-muted/50 p-3">
            <strong>{section.ifNothingDoneTitle}</strong>
            <p className="mt-1">{section.ifNothingDone}</p>
          </div>
        </div>
        <DialogFooter>
          <Button type="button" className="h-11 sm:h-9" onClick={onClose}>
            {t.common.close}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function HelpButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" className="help-button" onClick={onClick} aria-label={label} title={label}>
      ?
    </button>
  )
}

export function TabHelpHeader({ onHelp, label }: { onHelp: () => void; label: string }) {
  return (
    <div className="tab-help-row">
      <span className="tab-help-title">{label}</span>
      <HelpButton onClick={onHelp} label={label} />
    </div>
  )
}
