import type { ReactNode } from 'react'

export function ChartGradient({ id, color, area = false }: { id: string; color: string; area?: boolean }) {
  return <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stopColor={color} stopOpacity={area ? 0.2 : 1} />
    <stop offset="100%" stopColor={color} stopOpacity={area ? 0.015 : 0.65} />
  </linearGradient></defs>
}

export function ChartTooltip({ active, title, items, note }: {
  active?: boolean; title: ReactNode; items: { label: string; value: string; color: string }[]; note?: ReactNode
}) {
  if (!active || !items.length) return null
  return <div className="chart-tooltip">
    <div className="chart-tooltip-heading">{title}</div>
    <div className="chart-tooltip-values">{items.map((item, index) => <div key={index}>
      <span><i style={{ background: item.color }} />{item.label}</span><strong>{item.value}</strong>
    </div>)}</div>
    {note && <p className="chart-tooltip-note">{note}</p>}
  </div>
}
