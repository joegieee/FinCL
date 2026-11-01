import { useId, useState } from 'react'
import { ComposedChart, Line, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartTooltip } from '@/components/ChartDetails'
import { money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, currencyTick } from '@/lib/chart-theme'
import { dateLabel } from '@/lib/format'
import type { ForecastCurrency, ForecastPage } from '@/features/forecast/api'

const utc = (iso: string) => Date.parse(`${iso}T00:00:00Z`)
const monthTick = (time: number) => new Date(time).toLocaleDateString('en-SG', { month: 'short', timeZone: 'UTC' })

function monthStarts(from: number, to: number) {
  const ticks: number[] = []
  const cursor = new Date(from)
  cursor.setUTCDate(1)
  while (cursor.getTime() <= to) {
    if (cursor.getTime() >= from) ticks.push(cursor.getTime())
    cursor.setUTCMonth(cursor.getUTCMonth() + 1)
  }
  return ticks
}

/**
 * The forecast of one currency, opening at the last balance statements prove.
 * Pockets are independent timelines and are never added together, so unlike
 * currencies get a switch rather than a shared line.
 */
export function CashTimeline({ page, today, reportingCurrency }: { page: ForecastPage; today: string; reportingCurrency: string }) {
  const hatch = useId()
  const [chosen, setChosen] = useState('')
  const entries = Object.entries(page.by_currency)
  const pockets = entries.filter((entry): entry is [string, ForecastCurrency] => entry[1].available)
  const withoutForecast = entries.filter(([, pocket]) => !pocket.available).map(([currency]) => currency)
  if (!pockets.length) return null
  const [currency, pocket] = pockets.find(([code]) => code === chosen) ?? pockets.find(([code]) => code === reportingCurrency) ?? pockets[0]

  const data = [
    { time: utc(pocket.starting_balance_as_of), date: pocket.starting_balance_as_of, base: pocket.starting_balance, cautious: pocket.starting_balance },
    ...pocket.base.days.map((day, index) => ({ time: utc(day.date), date: day.date, base: day.closing, cautious: pocket.conservative.days[index]?.closing ?? null })),
  ].map(point => ({ ...point, basePlot: Number(point.base), cautiousPlot: point.cautious === null ? null : Number(point.cautious) }))
  const cutoff = data[0].time
  const end = data[data.length - 1].time
  const now = utc(today)

  return <figure className="ov-timeline">
    <figcaption>
      <ul className="ov-legend">
        <li><i className="is-base" />Expected <strong>{money(pocket.base.closing_balance, currency)}</strong></li>
        <li><i className="is-cautious" />If income doesn’t arrive <strong>{money(pocket.conservative.closing_balance, currency)}</strong></li>
        {now > cutoff && <li><i className="is-gap" />No statements yet</li>}
      </ul>
      {pockets.length > 1 && <div className="ov-pockets" role="group" aria-label="Currency">
        {pockets.map(([code]) => <button key={code} aria-pressed={code === currency} onClick={() => setChosen(code)}>{code}</button>)}
      </div>}
    </figcaption>
    <div role="img" aria-label={`${currency} forecast from ${dateLabel(pocket.starting_balance_as_of)} to ${dateLabel(data[data.length - 1].date)}. Expected closing ${money(pocket.base.closing_balance, currency)}; if income doesn’t arrive, ${money(pocket.conservative.closing_balance, currency)}.`}>
      <ResponsiveContainer width="100%" height={250}>
        <ComposedChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: 0 }}>
          <defs><pattern id={hatch} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="var(--muted)" strokeOpacity={0.35} strokeWidth="2" /></pattern></defs>
          <XAxis dataKey="time" type="number" scale="time" domain={[cutoff, end]} ticks={monthStarts(cutoff, end)} tickFormatter={monthTick} tick={CHART_AXIS_TICK} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={currencyTick(currency)} tick={CHART_AXIS_TICK} width={85} axisLine={false} tickLine={false} tickCount={4} />
          {now > cutoff && <ReferenceArea x1={cutoff} x2={Math.min(now, end)} fill={`url(#${hatch})`} stroke="none" ifOverflow="hidden" />}
          <ReferenceLine y={0} stroke={CHART_GRID_STROKE} strokeWidth={2} />
          <Tooltip
            cursor={{ stroke: 'var(--muted)', strokeDasharray: '3 3' }}
            content={({ active, payload }) => {
              const point = payload?.[0]?.payload as (typeof data)[number] | undefined
              return <ChartTooltip active={active} title={point ? dateLabel(point.date) : ''} items={point ? [
                { label: 'Expected', value: money(point.base, currency), color: CHART_SEMANTIC.base },
                { label: 'If income doesn’t arrive', value: money(point.cautious, currency), color: CHART_SEMANTIC.conservative },
              ] : []} />
            }}
          />
          <Line dataKey="basePlot" stroke={CHART_SEMANTIC.base} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line dataKey="cautiousPlot" stroke={CHART_SEMANTIC.conservative} strokeWidth={2} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
    {withoutForecast.length > 0 && <p className="ov-note">No forecast for {withoutForecast.join(', ')}.</p>}
  </figure>
}
