import { InfoIcon } from 'lucide-react'
import { useState } from 'react'
import { useT } from '../i18n'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

const STORAGE_KEY = 'bims-shopify-portal-auto-reconcile-banner-dismissed'

function loadDismissed(): boolean {
  if (typeof window === 'undefined') return false
  return window.localStorage.getItem(STORAGE_KEY) === '1'
}

export function AutoReconcileBanner({ onViewGuide }: { onViewGuide: () => void }) {
  const { t } = useT()
  const [dismissed, setDismissed] = useState(loadDismissed)

  if (dismissed) return null

  function dismiss() {
    setDismissed(true)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, '1')
    }
  }

  return (
    <Alert role="status" className="bg-muted/50">
      <InfoIcon />
      <AlertDescription className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <span className="text-foreground">{t.banner.text}</span>
        <span className="flex shrink-0 gap-2">
          <Button type="button" variant="outline" className="h-11 flex-1 sm:h-9 sm:flex-none" onClick={onViewGuide}>
            {t.common.viewGuide}
          </Button>
          <Button type="button" variant="ghost" className="h-11 flex-1 sm:h-9 sm:flex-none" onClick={dismiss}>
            {t.common.dismiss}
          </Button>
        </span>
      </AlertDescription>
    </Alert>
  )
}
