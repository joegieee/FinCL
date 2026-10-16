import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartFrame } from '@/components/ChartFrame'
import { money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, CHART_TOOLTIP_STYLE, currencyTick } from '@/lib/chart-theme'
import type { TrendMonthOut } from './reportedApi'

function monthLabel(month: string) {
  return new Date(`${month}-01T12:00:00`).toLocaleDateString('en', { month: 'short', year: 'numeric' })
}

export function spendingChangeLabel(row: TrendMonthOut, currency: string): string {
  const delta = row.spending_change?.value
  if (delta == null) return row.spending_change_reason ?? 'No comparison available.'
  const direction = Number(delta) > 0 ? 'rose' : Number(delta) < 0 ? 'fell' : 'was unchanged'
  if (Number(delta) === 0) return `Spending was unchanged from the previous month${row.spending_change_pct == null ? ' (previous spending was zero)' : ' (0.0%)'}.`
  const pct = row.spending_change_pct
  return `Spending ${direction} by ${money(delta.replace(/^-/, ''), currency)}${pct == null ? '' : ` (${pct.replace(/^-/, '')}%)`} from the previous month.${pct == null ? ' Previous spending was zero; no percentage comparison.' : ''}`
}

/** Amounts and percentages are computed server-side from exact valuations.
 * A gap invalidates only comparisons touching that month, never bridges it. */
export function SpendingComparisonChart({ trend, currency }: { trend: TrendMonthOut[]; currency: string }) {
  const [mode, setMode] = useState('spending')
  const latest = trend.at(-1)
  const data = trend.map(row => ({
    month: row.month,
    spending: row.complete && row.spending.total ? Number(row.spending.total.value) : null,
    previous: row.spending_change && row.previous_spending?.total ? Number(row.previous_spending.total.value) : null,
    change: row.spending_change ? Number(row.spending_change.value) : null,
    row,
  }))
  return <ChartFrame title="How has my spending changed?"
    caption={`Month-on-month · ${currency} · complete calendar months ending at the selected end date`}
    action={<label className="spending-comparison-mode">Chart view<select aria-label="Spending chart view" value={mode} onChange={event => setMode(event.target.value)}>
      <option value="spending">Monthly comparison</option><option value="change">Monthly change</option>
    </select></label>}
    ariaLabel={`Month-on-month spending ${mode === 'change' ? 'change' : 'comparison'} chart · ${currency}`}
    legend={mode === 'spending' ? <><span><i style={{ background: CHART_SEMANTIC.spending }} />This month</span><span><i style={{ background: CHART_SEMANTIC.conservative }} />Previous month</span></> : <span><i style={{ background: CHART_SEMANTIC.spending }} />Change from previous month</span>}
    footer="Gross purchases only; fees and refunds are separate. Comparisons require full coverage and valuation for both adjacent months. Gaps are unavailable data, not zero spending. Percentages use the previous month as the base."
    table={<table><thead><tr><th>Month</th><th>Purchases</th><th>Previous month</th><th>Change</th><th>Change %</th><th>Comparison</th></tr></thead><tbody>
      {trend.map(row => <tr key={row.month}><td>{monthLabel(row.month)}</td>
        <td>{row.complete ? money(row.spending.total?.value, currency) : 'Unavailable'}</td>
        <td>{money(row.previous_spending?.total?.value, currency)}</td>
        <td>{money(row.spending_change?.value, currency)}</td>
        <td>{row.spending_change_pct == null ? '—' : `${row.spending_change_pct}%`}</td>
        <td>{row.spending_change_reason ?? (row.spending_change ? 'Comparable' : 'No comparison available')}</td></tr>)}
    </tbody></table>}>
    {latest && <p className="spending-change-summary" role="status"><strong>{monthLabel(latest.month)}</strong> · {spendingChangeLabel(latest, currency)}</p>}
    {data.length ? <div className="money-bar-scroll" tabIndex={0} role="region" aria-label="Monthly spending scrollable plot"><div style={{ minWidth: Math.max(520, data.length * 100) }}>
      <ResponsiveContainer width="100%" height={290}>
        <BarChart data={data} margin={{ top: 12, right: 20, left: 12, bottom: 12 }} accessibilityLayer>
          <CartesianGrid vertical={false} stroke={CHART_GRID_STROKE} />
          <XAxis dataKey="month" tickFormatter={monthLabel} tick={CHART_AXIS_TICK} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={currencyTick(currency)} tick={CHART_AXIS_TICK} width={85} axisLine={false} tickLine={false} />
          <ReferenceLine y={0} stroke={CHART_GRID_STROKE} />
          <Tooltip contentStyle={CHART_TOOLTIP_STYLE} labelFormatter={label => monthLabel(String(label))}
            formatter={(_value, name, item) => {
              const row: TrendMonthOut = item.payload.row
              const value = item.dataKey === 'change' ? row.spending_change?.value : item.dataKey === 'previous' ? row.previous_spending?.total?.value : row.spending.total?.value
              return [money(value, currency), String(name)]
            }} />
          {mode === 'spending' ? <>
            <Bar dataKey="spending" name="This month" fill={CHART_SEMANTIC.spending} maxBarSize={32} isAnimationActive={false} />
            <Bar dataKey="previous" name="Previous month" fill={CHART_SEMANTIC.conservative} maxBarSize={32} isAnimationActive={false} />
          </> : <Bar dataKey="change" name="Change" fill={CHART_SEMANTIC.spending} maxBarSize={40} isAnimationActive={false} />}
        </BarChart>
      </ResponsiveContainer>
    </div></div> : <p className="spending-change-summary">No monthly spending history available.</p>}
  </ChartFrame>
}
