import { Route, Routes } from 'react-router-dom'
import { useT } from './i18n'
import { PortalPage } from './PortalPage'

function MissingSlug() {
  const { t } = useT()
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <p className="text-center text-sm text-muted-foreground">{t.common.missingSlug}</p>
    </main>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/portal/:slug" element={<PortalPage />} />
      <Route path="*" element={<MissingSlug />} />
    </Routes>
  )
}
