import { useEffect, useRef } from 'react'
import { useT } from '../i18n'
import { Badge } from '@/components/ui/badge'
import { TabsList, TabsTrigger } from '@/components/ui/tabs'

export interface PortalNavItem {
  value: string
  label: string
  count?: number
}

/**
 * Tab strip for the portal sections. On narrow screens it scrolls
 * horizontally inside its own container (never the page) and keeps the
 * active section scrolled into view; a fade on the right edge hints that
 * more sections exist off-screen.
 */
export function PortalNav({ items, active }: { items: PortalNavItem[]; active: string }) {
  const { t } = useT()
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const list = listRef.current
    const trigger = list?.querySelector<HTMLElement>('[data-state="active"]')
    if (!list || !trigger) return
    const target = trigger.offsetLeft - (list.clientWidth - trigger.offsetWidth) / 2
    list.scrollTo({ left: Math.max(0, target), behavior: 'smooth' })
  }, [active, items.length])

  return (
    <div className="relative">
      <TabsList
        ref={listRef}
        variant="line"
        aria-label={t.tabs.navLabel}
        className="h-12 w-full justify-start gap-1 overflow-x-auto rounded-none p-0 pr-8 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:pr-0"
      >
        {items.map((item) => (
          <TabsTrigger
            key={item.value}
            value={item.value}
            className="h-12 flex-none rounded-none px-3 group-data-horizontal/tabs:after:bottom-0 sm:px-4"
          >
            {item.label}
            {item.count !== undefined && (
              <Badge variant="secondary" className="px-1.5">
                {item.count}
              </Badge>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-background to-transparent sm:hidden"
      />
    </div>
  )
}
