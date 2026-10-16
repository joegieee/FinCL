import { useId, useState } from 'react'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Cell, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartFrame } from '@/components/ChartFrame'
import { ChartGradient, ChartTooltip } from '@/components/ChartDetails'
import { money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, currencyTick } from '@/lib/chart-theme'
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
  const gradient = useId()
  const latest = trend.at(-1)
  const direction = latest?.spending_change ? Math.sign(Number(latest.spending_change.value)) : null
  const DirectionIcon = direction === null || direction === 0 ? Minus : direction > 0 ? ArrowUpRight : ArrowDownRight
  const data = trend.map(row => ({
    month: row.month,
    spending: row.complete && row.spending.total ? Number(row.spending.total.value) : null,
    previous: row.spending_change && row.previous_spending?.total ? Number(row.previous_spending.total.value) : null,
    change: row.spending_change ? Number(row.spending_change.value) : null,
    row,
  }))
  return <ChartFrame title="How has my spending changed?" eyebrow="SPENDING OVER TIME"
    caption={`Month-on-month · ${currency} · complete calendar months ending at the selected end date`}
    action={<div className="chart-segmented" role="group" aria-label="Spending chart view">
      <button aria-pressed={mode === 'spending'} onClick={() => setMode('spending')}>Compare months</button>
      <button aria-pressed={mode === 'change'} onClick={() => setMode('change')}>Monthly change</button>
    </div>}
    insight={latest && <div className="spending-insight">
      <div className="chart-kpis">
        <div><span>{monthLabel(latest.month)} <small>Latest month</small></span><strong>{money(latest.complete ? latest.spending.total?.value : null, currency)}</strong></div>
        <div><span>Previous month</span><strong>{money(latest.previous_spending?.total?.value, currency)}</strong></div>
        <div className={direction === null ? '' : direction > 0 ? 'is-increase' : direction < 0 ? 'is-decrease' : ''}>
          <span>Month-on-month change</span><strong><DirectionIcon size={20} aria-hidden="true" />{money(latest.spending_change?.value, currency)}</strong>
          {latest.spending_change_pct != null && <small className="chart-change-badge">{Number(latest.spending_change_pct) > 0 ? '+' : ''}{latest.spending_change_pct}% vs previous month</small>}
        </div>
      </div>
      <p className="spending-change-summary" role="status"><strong>{monthLabel(latest.month)}</strong> · {spendingChangeLabel(latest, currency)}</p>
    </div>}
    ariaLabel={`Month-on-month spending ${mode === 'change' ? 'change' : 'comparison'} chart · ${currency}`}
    legend={mode === 'spending' ? <><span><i style={{ background: CHART_SEMANTIC.spending }} />This month</span><span><i style={{ background: CHART_SEMANTIC.previous }} />Previous month</span></> : <><span><i style={{ background: CHART_SEMANTIC.increase }} />Spending rose</span><span><i style={{ background: CHART_SEMANTIC.decrease }} />Spending fell</span></>}
    footer="Gross purchases only; fees and refunds are separate. Comparisons require full coverage and valuation for both adjacent months. Gaps are unavailable data, not zero spending. Percentages use the previous month as the base."
    table={<table><thead><tr><th>Month</th><th>Purchases</th><th>Previous month</th><th>Change</th><th>Change %</th><th>Comparison</th></tr></thead><tbody>
      {trend.map(row => <tr key={row.month}><td>{monthLabel(row.month)}</td>
        <td>{row.complete ? money(row.spending.total?.value, currency) : 'Unavailable'}</td>
        <td>{money(row.previous_spending?.total?.value, currency)}</td>
        <td>{money(row.spending_change?.value, currency)}</td>
        <td>{row.spending_change_pct == null ? '—' : `${row.spending_change_pct}%`}</td>
        <td>{row.spending_change_reason ?? (row.spending_change ? 'Comparable' : 'No comparison available')}</td></tr>)}
    </tbody></table>}>
    {data.length ? <div className="money-bar-scroll" tabIndex={0} role="region" aria-label="Monthly spending scrollable plot"><div style={{ minWidth: Math.max(520, data.length * 100) }}>
      <ResponsiveContainer width="100%" height={310}>
        <BarChart data={data} margin={{ top: 20, right: 24, left: 12, bottom: 16 }} barGap={5} accessibilityLayer>
          <ChartGradient id={`${gradient}-spending`} color={CHART_SEMANTIC.spending} />
          <ChartGradient id={`${gradient}-increase`} color={CHART_SEMANTIC.increase} />
          <ChartGradient id={`${gradient}-decrease`} color={CHART_SEMANTIC.decrease} />
          {latest && <ReferenceArea x1={latest.month} x2={latest.month} fill="var(--chart-hover)" strokeOpacity={0} />}
          <CartesianGrid vertical={false} stroke={CHART_GRID_STROKE} strokeDasharray="3 5" strokeOpacity={0.65} />
          <XAxis dataKey="month" tickFormatter={monthLabel} tick={CHART_AXIS_TICK} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={currencyTick(currency)} tick={CHART_AXIS_TICK} width={85} axisLine={false} tickLine={false} />
          <ReferenceLine y={0} stroke={CHART_GRID_STROKE} />
          <Tooltip filterNull={false} cursor={{ fill: 'var(--chart-hover)' }} content={({ active, label }) => {
            const row = trend.find(item => item.month === label)
            return <ChartTooltip active={active} title={row ? monthLabel(row.month) : ''} items={row ? [
              { label: 'Purchases', value: money(row.complete ? row.spending.total?.value : null, currency), color: CHART_SEMANTIC.spending },
              { label: 'Previous month', value: money(row.previous_spending?.total?.value, currency), color: CHART_SEMANTIC.previous },
              { label: 'Change', value: money(row.spending_change?.value, currency), color: Number(row.spending_change?.value ?? 0) > 0 ? CHART_SEMANTIC.increase : CHART_SEMANTIC.decrease },
            ] : []} note={row && spendingChangeLabel(row, currency)} />
          }} />
          {mode === 'spending' ? <>
            <Bar dataKey="spending" name="This month" fill={`url(#${gradient}-spending)`} radius={[5, 5, 0, 0]} maxBarSize={32} isAnimationActive={false} />
            <Bar dataKey="previous" name="Previous month" fill={CHART_SEMANTIC.previous} fillOpacity={0.45} radius={[5, 5, 0, 0]} maxBarSize={32} isAnimationActive={false} />
          </> : <Bar dataKey="change" name="Change" maxBarSize={40} radius={[5, 5, 5, 5]} isAnimationActive={false}>
            {data.map(row => <Cell key={row.month} fill={`url(#${gradient}-${(row.change ?? 0) > 0 ? 'increase' : 'decrease'})`} />)}
          </Bar>}
        </BarChart>
      </ResponsiveContainer>
    </div></div> : <p className="spending-change-summary">No monthly spending history available.</p>}
  </ChartFrame>
}
