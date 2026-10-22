import { useT } from '../i18n'
import type { Locale } from '../i18n/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const LOCALES: { value: Locale; label: string }[] = [
  { value: 'es', label: 'ES' },
  { value: 'en', label: 'EN' },
]

export function LanguageToggle({ className }: { className?: string }) {
  const { locale, setLocale, t } = useT()

  return (
    <div className={cn('flex gap-1', className)} role="group" aria-label={t.common.languageToggleLabel}>
      {LOCALES.map((option) => (
        <Button
          key={option.value}
          type="button"
          size="sm"
          variant={locale === option.value ? 'default' : 'outline'}
          aria-pressed={locale === option.value}
          className="h-11 min-w-12 flex-1 sm:h-9 sm:min-w-10 sm:flex-none"
          onClick={() => {
            if (option.value !== locale) setLocale(option.value)
          }}
        >
          {option.label}
        </Button>
      ))}
    </div>
  )
}
