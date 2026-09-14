import { Route, Routes } from 'react-router-dom'
import { PortalPage } from './PortalPage'

function MissingSlug() {
  return (
    <div className="centered-page">
      <p className="empty-state">Missing store slug. Use a link like /portal/your-store.</p>
    </div>
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
