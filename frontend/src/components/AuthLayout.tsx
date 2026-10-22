import type { ReactNode } from 'react'
import { LanguageToggle } from './LanguageToggle'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'

interface AuthLayoutProps {
  title: string
  description: string
  children: ReactNode
}

/** Centered card shared by the sign-in and access-key screens. */
export function AuthLayout({ title, description, children }: AuthLayoutProps) {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="gap-3">
          <div className="flex items-center justify-between gap-3">
            <h1 className="font-heading text-lg font-medium">{title}</h1>
            <LanguageToggle />
          </div>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </main>
  )
}
