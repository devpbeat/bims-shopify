import { CircleHelpIcon } from 'lucide-react'
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

export function TabHelpHeader({ onHelp, label }: { onHelp: () => void; label: string }) {
  const { t } = useT()
  return (
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="text-sm font-semibold">{label}</h2>
      <Button type="button" variant="outline" className="h-11 sm:h-9" onClick={onHelp}>
        <CircleHelpIcon />
        {t.common.viewGuide}
      </Button>
    </div>
  )
}
