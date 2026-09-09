import type { ReactNode } from 'react'
import { Card } from '@/components/ui/card'

/**
 * The supporting pattern every chart shares: eyebrow/title, an optional
 * caption and legend, the plotted visual, an exact-values `<details>` table,
 * and a footer. `ariaLabel` wraps `children` in `role="img"`; omit it when
 * the caller needs finer control over what carries that role (e.g. a chart
 * with an adjacent, independently-focusable list).
 */
export function ChartFrame({
  eyebrow, title, action, caption, legend, ariaLabel, footer, table, children, className,
}: {
  eyebrow?: string
  title: string
  action?: ReactNode
  caption?: string
  legend?: ReactNode
  ariaLabel?: string
  footer?: ReactNode
  table?: ReactNode
  children: ReactNode
  className?: string
}) {
  return <Card className={`chart-card${className ? ` ${className}` : ''}`}>
    <div className="panel-title"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>{action}</div>
    {caption && <p className="chart-caption">{caption}</p>}
    {legend && <div className="legend">{legend}</div>}
    {ariaLabel ? <div role="img" aria-label={ariaLabel}>{children}</div> : children}
    {table && <details className="chart-data"><summary>Exact values and coverage</summary>{table}</details>}
    {footer && <div className="card-foot">{footer}</div>}
  </Card>
}
