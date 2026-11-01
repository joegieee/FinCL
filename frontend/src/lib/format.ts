// One dateLabel for the whole app; previously redeclared identically in
// App.tsx, cards, debts, networth and obligations.
export function dateLabel(value: string): string {
  return new Date(value + 'T12:00:00').toLocaleDateString('en-SG', { month: 'short', day: 'numeric', year: 'numeric' })
}

/** A whole calendar month by name, anything else as its two dates. */
export function periodLabel(start: string, end: string): string {
  const first = new Date(start + 'T12:00:00')
  const next = new Date(end + 'T12:00:00')
  next.setDate(next.getDate() + 1)
  const wholeMonth = first.getDate() === 1 && next.getDate() === 1 && start.slice(0, 7) === end.slice(0, 7)
  return wholeMonth ? first.toLocaleDateString('en-SG', { month: 'long', year: 'numeric' }) : `${dateLabel(start)} – ${dateLabel(end)}`
}

export { money, shortCategory } from './api'
