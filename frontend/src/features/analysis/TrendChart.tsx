import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartFrame } from '@/components/ChartFrame'
import { money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, CHART_TOOLTIP_STYLE, currencyTick } from '@/lib/chart-theme'
import type { TrendMonthOut } from './reportedApi'

function cellValue(cell: TrendMonthOut['spending']): string | null {
  return cell.total?.value ?? cell.partial_total?.value ?? null
}

/**
 * A selected number of complete calendar months ending at the most recent
 * complete month whose end does not exceed the selected end. Gross-spending bars carry a separate,
 * labelled fees series; income is its own series. Gaps are explicit nulls, not
 * zero. The full range is shown so it cannot be read as the selected-period
 * total. Every cell is a reported, all-pocket consolidated value; a chart
 * bar's height is a display-only `Number()` read of the same exact decimal
 * the table row (and the `<ConsolidatedMoney>` cell in it) renders verbatim.
 */
export function TrendChart({
  trend, currency, range,
}: {
  trend: TrendMonthOut[]
  currency: string
  range: '6' | '12' | 'all'
}) {
  const data = trend.map(t => ({
    month: t.month,
    spending: t.complete ? Number(cellValue(t.spending) ?? '0') : null,
    fees: t.complete ? Number(cellValue(t.fees) ?? '0') : null,
    income: t.complete ? Number(cellValue(t.income) ?? '0') : null,
    complete: t.complete,
  }))
  const historyLabel = range === '6' ? 'SIX-MONTH VIEW'
    : range === '12' ? 'TWELVE-MONTH VIEW' : 'ALL COVERED HISTORY'
  const chartWidth = Math.max(520, data.length * 68)
  return (
    <ChartFrame
      eyebrow={historyLabel}
      title="Am I spending more than I earn?"
      caption={`Complete calendar months · ${currency} · not the selected-period total`}
      ariaLabel={`${historyLabel.toLowerCase()} spending, fees, and income chart`}
      footer="Gaps mean unavailable data, not zero activity."
      legend={<>
        <span><i style={{ background: CHART_SEMANTIC.spending }} />Purchases</span>
        <span><i style={{ background: CHART_SEMANTIC.fees }} />Fees</span>
        <span><i style={{ background: CHART_SEMANTIC.income }} />Income</span>
      </>}
      table={<table>
        <thead>
          <tr><th>Month</th><th>Purchases</th><th>Fees</th><th>Income</th><th>Coverage</th></tr>
        </thead>
        <tbody>
          {trend.map(t => (
            <tr key={t.month}>
              <td>{t.month}</td>
              <td>{t.complete ? money(cellValue(t.spending), currency) : '—'}</td>
              <td>{t.complete ? money(cellValue(t.fees), currency) : '—'}</td>
              <td>{t.complete ? money(cellValue(t.income), currency) : '—'}</td>
              <td>{t.complete ? 'Complete' : 'Missing period'}</td>
            </tr>
          ))}
        </tbody>
      </table>}
    >
      <div className="trend-chart">
        <div style={{ minWidth: chartWidth }}>
          <ResponsiveContainer width="100%" height={270}>
          <BarChart data={data} barGap={3} accessibilityLayer>
            <CartesianGrid vertical={false} stroke={CHART_GRID_STROKE} />
            <XAxis
              dataKey="month"
              tickFormatter={m => new Date(m + '-01T12:00:00').toLocaleDateString('en', { month: 'short' })}
              axisLine={false}
              tickLine={false}
              tick={CHART_AXIS_TICK}
            />
            <YAxis tickLine={false} axisLine={false} width={62} tick={CHART_AXIS_TICK} tickFormatter={currencyTick(currency)} />
            <Tooltip
              contentStyle={CHART_TOOLTIP_STYLE}
              formatter={(value, name) => [money(String(value), currency), String(name)]}
              labelFormatter={m => `${m}${data.find(d => d.month === m)?.complete ? '' : ' · missing coverage'}`}
            />
            <Bar dataKey="spending" name="Purchases" stackId="out" fill={CHART_SEMANTIC.spending} maxBarSize={30} isAnimationActive={false} />
            <Bar dataKey="fees" name="Fees" stackId="out" fill={CHART_SEMANTIC.fees} maxBarSize={30} isAnimationActive={false} />
            <Bar dataKey="income" name="Income" fill={CHART_SEMANTIC.income} radius={[4, 4, 0, 0]} maxBarSize={30} isAnimationActive={false} />
          </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </ChartFrame>
  )
}
