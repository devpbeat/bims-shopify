import type { PaymentDayAggregate } from '../api/types'
import { useT } from '../i18n'
import { useElementWidth } from '@/lib/useElementWidth'
import { fillDays, formatGuaranies, niceCeiling } from '../paymentsReportUtils'

interface PaymentsPerDayChartProps {
  byDay: PaymentDayAggregate[]
  dateFrom: string | null
  dateTo: string | null
}

const HEIGHT = 240
const FALLBACK_WIDTH = 320

/**
 * Daily payment volume. Driven by the server's `by_day` aggregate (never by
 * the visible page of rows) and zero-filled across the applied date window.
 * The SVG is drawn at the measured container width so text stays readable on
 * a phone instead of scaling down with a fixed viewBox.
 */
export function PaymentsPerDayChart({ byDay, dateFrom, dateTo }: PaymentsPerDayChartProps) {
  const { t } = useT()
  const [containerRef, width] = useElementWidth(FALLBACK_WIDTH)

  if (byDay.length === 0) return <p className="py-6 text-center text-sm text-muted-foreground">{t.paymentsReport.chartEmpty}</p>

  const compact = width < 480
  const margin = { top: 12, right: 8, bottom: 44, left: compact ? 64 : 84 }
  const days = fillDays(byDay, dateFrom, dateTo)
  const ceiling = niceCeiling(Math.max(...days.map((d) => d.total_amount)))
  const plotW = Math.max(1, width - margin.left - margin.right)
  const plotH = HEIGHT - margin.top - margin.bottom
  const slot = plotW / days.length
  const barW = Math.max(1, Math.min(28, slot * 0.7))
  const yFor = (value: number) => margin.top + plotH - (value / ceiling) * plotH
  const ticks = [0, ceiling / 2, ceiling]
  // Label first, last and at most a handful of evenly spaced days so labels never overlap.
  const maxLabels = Math.max(2, Math.floor(plotW / 56))
  const labelEvery = Math.max(1, Math.ceil(days.length / maxLabels))

  return (
    <div ref={containerRef} className="w-full">
      <p className="text-xs text-muted-foreground">{t.paymentsReport.chartAxisAmount}</p>
      <svg width={width} height={HEIGHT} role="img" aria-label={t.paymentsReport.chartTitle} className="block max-w-full">
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={margin.left} x2={width - margin.right} y1={yFor(tick)} y2={yFor(tick)} className="stroke-border" strokeWidth={1} />
            <text x={margin.left - 6} y={yFor(tick) + 4} textAnchor="end" className="fill-muted-foreground text-[11px]">
              {formatGuaranies(tick)}
            </text>
          </g>
        ))}
        {days.map((day, index) => {
          const x = margin.left + slot * index + (slot - barW) / 2
          const y = yFor(day.total_amount)
          return (
            <g key={day.date}>
              <rect x={x} y={y} width={barW} height={margin.top + plotH - y} rx={2} className="fill-primary">
                <title>
                  {day.date} · {formatGuaranies(day.total_amount)} PYG · {day.count} {t.paymentsReport.chartCountLabel}
                </title>
              </rect>
              {(index % labelEvery === 0 || index === days.length - 1) && (
                <text x={x + barW / 2} y={HEIGHT - margin.bottom + 16} textAnchor="middle" className="fill-muted-foreground text-[11px]">
                  {day.date.slice(5)}
                </text>
              )}
            </g>
          )
        })}
        <text x={margin.left + plotW / 2} y={HEIGHT - 6} textAnchor="middle" className="fill-muted-foreground text-[11px]">
          {t.paymentsReport.chartAxisDate}
        </text>
      </svg>
    </div>
  )
}
