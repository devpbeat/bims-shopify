import { useT } from '../i18n'
import type { Locale } from '../i18n/types'

export function LanguageToggle() {
  const { locale, setLocale, t } = useT()

  function toggle(next: Locale) {
    if (next !== locale) setLocale(next)
  }

  return (
    <div className="language-toggle" role="group" aria-label={t.common.languageToggleLabel}>
      <button
        type="button"
        className={locale === 'es' ? 'lang-btn active' : 'lang-btn'}
        onClick={() => toggle('es')}
      >
        ES
      </button>
      <button
        type="button"
        className={locale === 'en' ? 'lang-btn active' : 'lang-btn'}
        onClick={() => toggle('en')}
      >
        EN
      </button>
    </div>
  )
}
