import type { PaymentDayAggregate } from '../api/types'
import { useT } from '../i18n'
import { fillDays, formatGuaranies, niceCeiling } from '../paymentsReportUtils'

interface PaymentsPerDayChartProps {
  byDay: PaymentDayAggregate[]
  dateFrom: string | null
  dateTo: string | null
}

const WIDTH = 720
const HEIGHT = 260
const MARGIN = { top: 12, right: 12, bottom: 48, left: 84 }

export function PaymentsPerDayChart({ byDay, dateFrom, dateTo }: PaymentsPerDayChartProps) {
  const { t } = useT()

  if (byDay.length === 0) return <p className="empty-state">{t.paymentsReport.chartEmpty}</p>

  const days = fillDays(byDay, dateFrom, dateTo)
  const ceiling = niceCeiling(Math.max(...days.map((d) => d.total_amount)))
  const plotW = WIDTH - MARGIN.left - MARGIN.right
  const plotH = HEIGHT - MARGIN.top - MARGIN.bottom
  const slot = plotW / days.length
  const barW = Math.max(1, Math.min(28, slot * 0.7))
  const yFor = (value: number) => MARGIN.top + plotH - (value / ceiling) * plotH
  const ticks = [0, ceiling / 2, ceiling]
  // Label first, last and at most ~6 evenly spaced days so labels never overlap.
  const labelEvery = Math.max(1, Math.ceil(days.length / 6))

  return (
    <svg
      className="payments-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={t.paymentsReport.chartTitle}
    >
      {ticks.map((tick) => (
        <g key={tick}>
          <line x1={MARGIN.left} x2={WIDTH - MARGIN.right} y1={yFor(tick)} y2={yFor(tick)} className="payments-chart-grid" />
          <text x={MARGIN.left - 8} y={yFor(tick) + 4} textAnchor="end" className="payments-chart-tick">
            {formatGuaranies(tick)}
          </text>
        </g>
      ))}
      {days.map((day, index) => {
        const x = MARGIN.left + slot * index + (slot - barW) / 2
        const y = yFor(day.total_amount)
        return (
          <g key={day.date}>
            <rect x={x} y={y} width={barW} height={MARGIN.top + plotH - y} className="payments-chart-bar">
              <title>
                {day.date} · {formatGuaranies(day.total_amount)} PYG · {day.count} {t.paymentsReport.chartCountLabel}
              </title>
            </rect>
            {(index % labelEvery === 0 || index === days.length - 1) && (
              <text x={x + barW / 2} y={HEIGHT - MARGIN.bottom + 16} textAnchor="middle" className="payments-chart-tick">
                {day.date.slice(5)}
              </text>
            )}
          </g>
        )
      })}
      <text x={MARGIN.left + plotW / 2} y={HEIGHT - 6} textAnchor="middle" className="payments-chart-axis">
        {t.paymentsReport.chartAxisDate}
      </text>
      <text
        transform={`translate(14 ${MARGIN.top + plotH / 2}) rotate(-90)`}
        textAnchor="middle"
        className="payments-chart-axis"
      >
        {t.paymentsReport.chartAxisAmount}
      </text>
    </svg>
  )
}
