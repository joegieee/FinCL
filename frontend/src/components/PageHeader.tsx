import type { ReactNode } from 'react'
import type { TabMeta } from '@/lib/tabs'

// Driven by the Phase 1 tab metadata table instead of App.tsx's own ternary
// chain: a tab's heading lives in exactly one place.
export function PageHeader({ meta, action }: { meta: TabMeta; action?: ReactNode }) {
  return <div className="page-heading">
    <div>{meta.eyebrow && <p className="eyebrow">{meta.eyebrow}</p>}<h1>{meta.title}</h1><p className="subtitle">{meta.subtitle}</p></div>
    {action}
  </div>
}
