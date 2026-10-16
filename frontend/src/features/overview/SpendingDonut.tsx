import { ArrowRight, BarChart3, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { ChartFrame } from '@/components/ChartFrame'
import { ChartTooltip } from '@/components/ChartDetails'
import { money, shortCategory, type Dashboard } from '@/lib/api'
import { CHART_CATEGORICAL, CHART_OTHER, CHART_TOOLTIP_STYLE, chartTint } from '@/lib/chart-theme'

const TOP_SLICES = 8

type Total = Dashboard['category_totals'][number]
type Slice = { name: string; value: string; plot: number; color: string; other?: boolean; members?: Total[] }

/**
 * Past the top eight categories, everything is grouped into one "Other"
 * slice so a colour is never reused and every rendered slice has a named,
 * clickable row in the list beside it.
 */
function buildSlices(totals: Total[]): Slice[] {
  const sorted = [...totals].sort((a, b) => Number(b.value) - Number(a.value))
  const top = sorted.slice(0, TOP_SLICES).map((t, i) => ({ name: t.name, value: t.value, plot: Number(t.value), color: CHART_CATEGORICAL[i % CHART_CATEGORICAL.length] }))
  const rest = sorted.slice(TOP_SLICES)
  if (!rest.length) return top
  const otherTotal = rest.reduce((sum, t) => sum + Number(t.value), 0)
  return [...top, { name: 'Other', value: String(otherTotal), plot: otherTotal, color: CHART_OTHER, other: true, members: rest }]
}

function DonutTooltip({ active, payload, currency }: { active?: boolean; payload?: Array<{ payload: Slice }>; currency: string }) {
  if (!active || !payload?.length) return null
  const item = payload[0].payload
  if (item.other && item.members) {
    return <div style={CHART_TOOLTIP_STYLE} className="donut-tooltip-other">
      <strong>Other ({item.members.length} categories)</strong>
      <ul>{item.members.map(m => <li key={m.name}><span>{shortCategory(m.name)}</span><span>{money(m.value, currency)}</span></li>)}</ul>
    </div>
  }
  return <ChartTooltip active title={shortCategory(item.name)} items={[{ label: 'Purchases', value: money(item.value, currency), color: item.color }]} />
}

export function SpendingDonut({ data, currency, spending, onOpenAnalysis, onDrill }: {
  data: Dashboard; currency: string; spending?: string | null; onOpenAnalysis: () => void; onDrill: (category: string) => void
}) {
  const slices = buildSlices(data.category_totals)
  return <ChartFrame
    eyebrow="SPENDING BREAKDOWN"
    title="Where did my money go?"
    action={<Button variant="ghost" size="sm" onClick={onOpenAnalysis}>Explore analysis <ArrowRight size={14} /></Button>}
    footer="Gross purchases by category. Fees are separate; refunds do not silently reduce category spending."
  >
    {slices.length ? <div className="spending-layout">
      <div className="donut-wrap" role="img" aria-label="Spending by category; exact values follow in the category list">
        <ResponsiveContainer width="100%" height={270}>
          <PieChart>
            <Pie data={slices} dataKey="plot" nameKey="name" innerRadius={88} outerRadius={112} paddingAngle={3} cornerRadius={5} stroke="none" isAnimationActive={false}
              onClick={(entry: unknown) => { const slice = entry as Slice; slice.other ? onOpenAnalysis() : onDrill(slice.name) }}>
              {slices.map(slice => <Cell key={slice.name} fill={slice.color} cursor="pointer" />)}
            </Pie>
            <Tooltip content={(props: unknown) => <DonutTooltip {...(props as { active?: boolean; payload?: Array<{ payload: Slice }> })} currency={currency} />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="donut-center"><span>Spent this period</span><strong>{money(spending, currency)}</strong></div>
      </div>
      <div className="category-list">
        {slices.map(slice => <button key={slice.name} onClick={() => slice.other ? onOpenAnalysis() : onDrill(slice.name)}>
          <span className="dot" style={{ background: slice.color }} />
          <span>{shortCategory(slice.name)}<small>{slice.other ? `${slice.members?.length ?? 0} more categories` : `${data.category_totals.find(t => t.name === slice.name)?.count ?? 0} transactions`}</small><span className="category-share-track" aria-hidden="true"><i style={{ background: slice.color, width: `${Math.min(100, slice.plot / (slices.reduce((sum, s) => sum + s.plot, 0) || 1) * 100)}%` }} /></span></span>
          <strong>{money(slice.value, currency)}</strong><ChevronRight size={14} />
        </button>)}
      </div>
    </div> : <div className="empty"><BarChart3 size={25} /><h3>{data.covered ? 'No spending in this period' : 'Spending chart unavailable'}</h3><p>Your category breakdown appears here when there are eligible transactions in a covered period.</p></div>}
  </ChartFrame>
}

export function categoryIconColor(index: number) {
  const color = CHART_CATEGORICAL[index % CHART_CATEGORICAL.length]
  return { background: chartTint(color), color }
}
