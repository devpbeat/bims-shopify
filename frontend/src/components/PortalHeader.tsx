import { MenuIcon } from 'lucide-react'
import { useState } from 'react'
import type { SyncStatus } from '../api/types'
import { useT } from '../i18n'
import { LanguageToggle } from './LanguageToggle'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { Separator } from '@/components/ui/separator'

interface PortalHeaderProps {
  slug: string
  syncStatus: SyncStatus | null
  onLogout: () => void
  isOperator: boolean
  onOperatorClick: () => void
}

export function PortalHeader({
  slug,
  syncStatus,
  onLogout,
  isOperator,
  onOperatorClick,
}: PortalHeaderProps) {
  const { t, locale } = useT()
  const [menuOpen, setMenuOpen] = useState(false)

  function formatDate(value: string | null | undefined): string {
    if (!value) return t.common.never
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return date.toLocaleString(locale === 'es' ? 'es-419' : 'en-US')
  }

  const operatorLabel = isOperator ? t.operator.exit : t.operator.entryLabel

  return (
    <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold capitalize leading-tight sm:text-xl">{slug}</h1>
          <p className="truncate text-xs text-muted-foreground sm:text-sm">
            {t.common.lastSync}: {formatDate(syncStatus?.last_run_at as string | undefined)}
          </p>
        </div>

        <div className="hidden items-center gap-2 md:flex">
          <LanguageToggle />
          <Button type="button" variant="ghost" onClick={onOperatorClick}>
            {operatorLabel}
          </Button>
          <Button type="button" variant="outline" onClick={onLogout}>
            {t.common.logout}
          </Button>
        </div>

        <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
          <SheetTrigger asChild>
            <Button type="button" variant="outline" size="icon" className="size-11 md:hidden" aria-label={t.common.openMenu}>
              <MenuIcon />
            </Button>
          </SheetTrigger>
          <SheetContent side="right" className="pb-[env(safe-area-inset-bottom)]">
            <SheetHeader>
              <SheetTitle>{t.common.menu}</SheetTitle>
              <SheetDescription>{t.common.menuDescription}</SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-4 px-4">
              <LanguageToggle />
              <Separator />
              <Button
                type="button"
                variant="outline"
                className="h-11 justify-start"
                onClick={() => {
                  setMenuOpen(false)
                  onOperatorClick()
                }}
              >
                {operatorLabel}
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-11 justify-start"
                onClick={() => {
                  setMenuOpen(false)
                  onLogout()
                }}
              >
                {t.common.logout}
              </Button>
            </div>
          </SheetContent>
        </Sheet>
      </div>
    </header>
  )
}
