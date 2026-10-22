import { useId } from 'react'
import type { ReactNode } from 'react'
import { Label } from '@/components/ui/label'

interface FieldControlProps {
  id: string
  'aria-describedby'?: string
}

interface FieldProps {
  label: ReactNode
  hint?: string
  children: (control: FieldControlProps) => ReactNode
}

/** Label + control + optional hint, wired together with matching ids. */
export function Field({ label, hint, children }: FieldProps) {
  const id = useId()
  const hintId = hint ? `${id}-hint` : undefined
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children({ id, 'aria-describedby': hintId })}
      {hint && (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  )
}
