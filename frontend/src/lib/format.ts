// One dateLabel for the whole app; previously redeclared identically in
// App.tsx, cards, debts, networth and obligations.
export function dateLabel(value: string): string {
  return new Date(value + 'T12:00:00').toLocaleDateString('en-SG', { month: 'short', day: 'numeric', year: 'numeric' })
}

export { money, shortCategory } from './api'
