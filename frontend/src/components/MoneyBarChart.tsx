import { useId } from 'react'
import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartFrame } from './ChartFrame'
import { ChartGradient, ChartTooltip } from './ChartDetails'
import { money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, currencyTick } from '@/lib/chart-theme'

export type MoneyBar = { label: string; value: string | null; detail?: string }

/** Decimal strings stay authoritative; Number is used only for SVG geometry. */
export function MoneyBarChart({ title, caption, currency, rows, footer, showTable = true }: {
  title: string; caption: string; currency: string; rows: MoneyBar[]; footer: string; showTable?: boolean
}) {
  const gradient = useId()
  const data = rows.map((row, index) => ({ ...row, index, amount: row.value !== null && Number.isFinite(Number(row.value)) ? Number(row.value) : null }))
  return <ChartFrame title={title} caption={caption} ariaLabel={`${title} · ${currency}`} footer={footer}
    table={showTable && <table><thead><tr><th>Item</th><th>Amount ({currency})</th><th>Basis</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}><td>{row.label}</td><td>{row.value === null ? 'Unavailable' : money(row.value, currency)}</td><td>{row.detail ?? 'Available'}</td></tr>)}</tbody></table>}>
    {rows.length === 0 ? <p className="muted-note">No values to chart.</p> : <div className="money-bar-scroll" tabIndex={0} role="region" aria-label={`${title} scrollable plot`}>
      <div style={{ minWidth: Math.max(340, rows.length * 85) }}>
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data} margin={{ top: 24, right: 24, bottom: 20, left: 12 }} accessibilityLayer>
            <ChartGradient id={gradient} color={CHART_SEMANTIC.base} />
            <CartesianGrid vertical={false} stroke={CHART_GRID_STROKE} strokeDasharray="3 5" strokeOpacity={0.65} />
            <XAxis dataKey="index" tickFormatter={index => { const label = rows[index]?.label ?? ''; return label.length > 12 ? `${label.slice(0, 11)}…` : label }} tick={CHART_AXIS_TICK} axisLine={false} tickLine={false} interval={0} />
            <YAxis tickFormatter={currencyTick(currency)} tick={CHART_AXIS_TICK} width={85} axisLine={false} tickLine={false} />
            <ReferenceLine y={0} stroke={CHART_GRID_STROKE} />
            <Tooltip cursor={{ fill: 'var(--chart-hover)' }} content={({ active, label }) => {
              const row = rows[Number(label)]
              return <ChartTooltip active={active} title={row?.label} items={row ? [{ label: 'Amount', value: money(row.value, currency), color: CHART_SEMANTIC.base }] : []} note={row?.detail} />
            }} />
            <Bar dataKey="amount" fill={`url(#${gradient})`} maxBarSize={44} radius={[6, 6, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>}
  </ChartFrame>
}
