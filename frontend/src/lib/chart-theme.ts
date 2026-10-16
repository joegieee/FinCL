/**
 * One shared palette for every chart, defined as CSS custom properties in
 * `index.css` so light and dark are both declared there and no chart carries
 * its own hardcoded hex literal.
 */
export const CHART_CATEGORICAL = [
  'var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)',
  'var(--chart-5)', 'var(--chart-6)', 'var(--chart-7)', 'var(--chart-8)',
] as const

export const CHART_OTHER = 'var(--muted)'

export const CHART_SEMANTIC = {
  spending: CHART_CATEGORICAL[0],
  fees: CHART_CATEGORICAL[3],
  income: CHART_CATEGORICAL[4],
  base: 'var(--chart-base)',
  conservative: 'var(--chart-conservative)',
  previous: 'var(--chart-comparison)',
  increase: 'var(--chart-increase)',
  decrease: 'var(--chart-decrease)',
} as const

export const CHART_TOOLTIP_STYLE: Record<string, string | number> = {
  background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 12,
  color: 'var(--foreground)', fontSize: 12, padding: '12px 16px',
  boxShadow: '0 8px 30px var(--shadow-faint)',
}

export const CHART_AXIS_TICK = { fill: 'var(--muted)', fontSize: 11 } as const

export const CHART_GRID_STROKE = 'var(--border)'

/** A background tint of a chart colour, for non-SVG surfaces (icon chips, etc). */
export function chartTint(colorVar: string, percent = 18) {
  return `color-mix(in srgb, ${colorVar} ${percent}%, transparent)`
}

export function abbreviateAmount(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}k`
  return String(Math.round(value))
}

/** A recharts axis tickFormatter: currency-aware, abbreviated. */
export function currencyTick(currency: string) {
  return (value: number) => `${currency} ${abbreviateAmount(value)}`
}
