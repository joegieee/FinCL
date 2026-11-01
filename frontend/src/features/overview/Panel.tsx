import type { ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/card'

export function Panel({ title, onOpen, action, className = '', children }: { title: string; onOpen: () => void; action?: ReactNode; className?: string; children: ReactNode }) {
  return <Card className={`ov-panel ${className}`.trim()}>
    <div className="ov-panel-head"><h3><button className="ov-panel-title" onClick={onOpen}>{title}<ChevronRight size={15} aria-hidden="true" /></button></h3>{action}</div>
    {children}
  </Card>
}

export type Bar = { key: string; label: ReactNode; size: number | null; value: ReactNode; className?: string }

/** Bars are drawn from `size`, which is geometry only; `value` is the figure the server sent. */
export function Bars({ rows }: { rows: Bar[] }) {
  const largest = Math.max(0, ...rows.map(row => row.size ?? 0))
  return <ul className="ov-bars">
    {rows.map(row => <li key={row.key} className={row.className}>
      <span className="ov-bar-label">{row.label}</span>
      <span className="ov-bar-value">{row.value}</span>
      <span className="ov-bar-track" aria-hidden="true"><i style={{ width: `${largest && row.size ? row.size / largest * 100 : 0}%` }} /></span>
    </li>)}
  </ul>
}

export function plotted(result: { total: { value: string } | null; partial_total: { value: string } | null }) {
  const value = result.total?.value ?? result.partial_total?.value
  return value == null ? null : Math.abs(Number(value))
}
