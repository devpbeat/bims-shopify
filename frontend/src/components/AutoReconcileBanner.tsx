import { useState } from 'react'
import { useT } from '../i18n'

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
    <div className="info-banner">
      <p>{t.banner.text}</p>
      <div className="info-banner-actions">
        <button type="button" className="btn btn-secondary" onClick={onViewGuide}>
          {t.common.viewGuide}
        </button>
        <button type="button" className="btn-link" onClick={dismiss}>
          {t.common.dismiss}
        </button>
      </div>
    </div>
  )
}
