import type { ReactNode } from 'react'
import { Card } from '@/components/ui/card'
import { ChartNoAxesCombined, ChevronDown, Info, Table2 } from 'lucide-react'

/**
 * The supporting pattern every chart shares: eyebrow/title, an optional
 * caption and legend, the plotted visual, an exact-values `<details>` table,
 * and a footer. `ariaLabel` wraps `children` in `role="img"`; omit it when
 * the caller needs finer control over what carries that role (e.g. a chart
 * with an adjacent, independently-focusable list).
 */
export function ChartFrame({
  eyebrow, title, action, caption, legend, ariaLabel, footer, table, children, className, insight,
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
  insight?: ReactNode
}) {
  return <Card className={`chart-card${className ? ` ${className}` : ''}`}>
    <div className="panel-title chart-heading"><div className="chart-heading-main"><span className="chart-heading-icon" aria-hidden="true"><ChartNoAxesCombined size={18} /></span><div><p className="eyebrow">{eyebrow ?? 'Financial insights'}</p><h2>{title}</h2></div></div>{action}</div>
    {caption && <p className="chart-caption">{caption}</p>}
    {insight}
    {legend && <div className="legend">{legend}</div>}
    {ariaLabel ? <div role="img" aria-label={ariaLabel}>{children}</div> : children}
    {table && <details className="chart-data"><summary><Table2 size={14} aria-hidden="true" />Exact values and coverage<ChevronDown size={14} aria-hidden="true" /></summary><div className="chart-table-scroll">{table}</div></details>}
    {footer && <div className="card-foot"><Info size={13} aria-hidden="true" /><span>{footer}</span></div>}
  </Card>
}
