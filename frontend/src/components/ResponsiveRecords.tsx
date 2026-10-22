import type { ReactNode } from 'react'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { cn } from '@/lib/utils'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

export interface RecordColumn<T> {
  id: string
  header: string
  cell: (row: T) => ReactNode
  /** Right-aligned, tabular figures (amounts and counts). */
  numeric?: boolean
  /** Used as the heading of the stacked card; defaults to the first column. */
  cardTitle?: boolean
}

interface ResponsiveRecordsProps<T> {
  rows: T[]
  columns: RecordColumn<T>[]
  getKey: (row: T) => string | number
  /** Accessible name of the table / list. */
  label: string
  /** Per-row controls, rendered as the last table column or the card footer. */
  actions?: (row: T) => ReactNode
  actionsHeader?: string
  className?: string
}

/**
 * Renders records as a table when there is room (>= 768px) and as stacked
 * cards below that, so nothing ever scrolls horizontally on a phone. Only one
 * of the two layouts is mounted at a time.
 */
export function ResponsiveRecords<T>({ rows, columns, getKey, label, actions, actionsHeader, className }: ResponsiveRecordsProps<T>) {
  const wide = useMediaQuery('(min-width: 768px)')

  if (wide) {
    return (
      <Table aria-label={label} className={className}>
        <TableHeader>
          <TableRow>
            {columns.map((column) => (
              <TableHead key={column.id} className={cn(column.numeric && 'text-right')}>
                {column.header}
              </TableHead>
            ))}
            {actions && (
              <TableHead>
                <span className="sr-only">{actionsHeader}</span>
              </TableHead>
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={getKey(row)}>
              {columns.map((column) => (
                <TableCell key={column.id} className={cn(column.numeric && 'text-right tabular-nums')}>
                  {column.cell(row)}
                </TableCell>
              ))}
              {actions && <TableCell>{actions(row)}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )
  }

  const titleColumn = columns.find((column) => column.cardTitle) ?? columns[0]
  const detailColumns = columns.filter((column) => column !== titleColumn)

  return (
    <ul aria-label={label} className={cn('flex flex-col gap-3', className)}>
      {rows.map((row) => (
        <li key={getKey(row)} className="flex flex-col gap-3 rounded-lg border bg-background p-3">
          <div className="min-w-0 font-medium break-words">{titleColumn.cell(row)}</div>
          <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] items-baseline gap-x-4 gap-y-2 text-sm">
            {detailColumns.map((column) => (
              <div key={column.id} className="col-span-2 grid grid-cols-subgrid items-baseline">
                <dt className="text-muted-foreground">{column.header}</dt>
                <dd className={cn('min-w-0 break-words', column.numeric && 'tabular-nums')}>{column.cell(row)}</dd>
              </div>
            ))}
          </dl>
          {actions && <div className="flex flex-col gap-2 sm:flex-row">{actions(row)}</div>}
        </li>
      ))}
    </ul>
  )
}
