import { cloneElement, useId, type ReactElement } from 'react'

// Pure-CSS hover/focus tooltip: no `title=`, so it is reachable by keyboard
// and touch focus, not just mouse hover (PLAN-ui-cohesion.md §1.6).
export function Tooltip({ label, children }: { label: string; children: ReactElement<{ 'aria-describedby'?: string }> }) {
  const id = useId()
  return <span className="tooltip-anchor">
    {cloneElement(children, { 'aria-describedby': id })}
    <span role="tooltip" id={id} className="tooltip-bubble">{label}</span>
  </span>
}
