import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Card } from '@/components/ui/card'
import { money, type TrendPoint } from '@/lib/api'

const SPEND = '#247b69'
const FEE = '#c8aa70'
const INCOME = '#809ba6'

/**
 * Six complete calendar months ending at the most recent complete month whose
 * end does not exceed the selected end. Gross-spending bars carry a separate,
 * labelled fees series; income is its own series. Gaps are explicit nulls, not
 * zero. The full range is shown so it cannot be read as the selected-period
 * total.
 */
export function TrendChart({ trend, currency }: { trend: TrendPoint[]; currency: string }) {
  const data = trend.map(t => ({
    month: t.month,
    spending: t.spending ? Number(t.spending.value) : null,
    fees: t.fees ? Number(t.fees.value) : null,
    income: t.income ? Number(t.income.value) : null,
    complete: t.complete,
  }))
  return (
    <Card className="chart-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">SIX-MONTH VIEW</p>
          <h2>Am I spending more than I earn?</h2>
        </div>
      </div>
      <p className="chart-caption">Complete calendar months · {currency} · not the selected-period total</p>
      <div className="legend">
        <span><i style={{ background: SPEND }} />Purchases</span>
        <span><i style={{ background: FEE }} />Fees</span>
        <span><i style={{ background: INCOME }} />Income</span>
      </div>
      <div className="trend-chart">
        <ResponsiveContainer width="100%" height={270}>
          <BarChart data={data} barGap={3} accessibilityLayer>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis
              dataKey="month"
              tickFormatter={m => new Date(m + '-01T12:00:00').toLocaleDateString('en', { month: 'short' })}
              axisLine={false}
              tickLine={false}
            />
            <YAxis tickLine={false} axisLine={false} width={62} />
            <Tooltip
              formatter={(value, name) => [money(String(value), currency), String(name)]}
              labelFormatter={m => `${m}${data.find(d => d.month === m)?.complete ? '' : ' · missing coverage'}`}
            />
            <Bar dataKey="spending" name="Purchases" stackId="out" fill={SPEND} maxBarSize={30} isAnimationActive={false} />
            <Bar dataKey="fees" name="Fees" stackId="out" fill={FEE} maxBarSize={30} isAnimationActive={false} />
            <Bar dataKey="income" name="Income" fill={INCOME} radius={[4, 4, 0, 0]} maxBarSize={30} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <details className="chart-data">
        <summary>Exact values and coverage</summary>
        <table>
          <thead>
            <tr><th>Month</th><th>Purchases</th><th>Fees</th><th>Income</th><th>Coverage</th></tr>
          </thead>
          <tbody>
            {trend.map(t => (
              <tr key={t.month}>
                <td>{t.month}</td>
                <td>{money(t.spending?.value ?? null, currency)}</td>
                <td>{money(t.fees?.value ?? null, currency)}</td>
                <td>{money(t.income?.value ?? null, currency)}</td>
                <td>{t.complete ? 'Complete' : 'Missing period'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <div className="card-foot">Gaps mean unavailable data, not zero activity.</div>
    </Card>
  )
}
