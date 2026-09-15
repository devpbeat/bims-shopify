import { useT } from '../i18n'
import type { HelpSection } from '../i18n/types'

interface HelpModalProps {
  section: HelpSection
  onClose: () => void
}

export function HelpModal({ section, onClose }: HelpModalProps) {
  const { t } = useT()

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={section.title} onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h2>
            {section.title} {t.help.modalTitleSuffix}
          </h2>
          <button type="button" className="btn-link modal-close" onClick={onClose} aria-label={t.common.close}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <p>{section.what}</p>
          <p>{section.howAutoResolves}</p>
          <p>{section.manualActions}</p>
          <div className="modal-highlight">
            <strong>{section.ifNothingDoneTitle}</strong>
            <p>{section.ifNothingDone}</p>
          </div>
        </div>
        <div className="modal-footer">
          <button type="button" className="btn btn-primary" onClick={onClose}>
            {t.common.close}
          </button>
        </div>
      </div>
    </div>
  )
}

export function HelpButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" className="help-button" onClick={onClick} aria-label={label} title={label}>
      ?
    </button>
  )
}

export function TabHelpHeader({ onHelp, label }: { onHelp: () => void; label: string }) {
  return (
    <div className="tab-help-row">
      <span className="tab-help-title">{label}</span>
      <HelpButton onClick={onHelp} label={label} />
    </div>
  )
}
