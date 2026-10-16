import { afterEach, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { SpendingComparisonChart, spendingChangeLabel } from './SpendingComparisonChart'
import type { TrendMonthOut } from './reportedApi'

afterEach(cleanup)

function month(over: Partial<TrendMonthOut> = {}): TrendMonthOut {
  const cell = (value: string) => ({ total: { value, currency: 'SGD' } })
  return { month: '2026-02', complete: true, spending: cell('27.50'), previous_spending: cell('50.00'),
    spending_change: { value: '-22.50', currency: 'SGD' }, spending_change_pct: '-45.0', spending_change_reason: null,
    ...over } as TrendMonthOut
}

it('explains a fall and preserves server-provided amounts and percentages in the table', () => {
  render(<SpendingComparisonChart currency="SGD" trend={[month()]} />)
  expect(screen.getByRole('status')).toHaveTextContent('Feb 2026 · Spending fell by SGD 22.50 (45.0%) from the previous month.')
  const row = screen.getByText('Feb 2026', { selector: 'td' }).closest('tr')!
  expect(within(row).getByText('−SGD 22.50')).toBeInTheDocument()
  expect(within(row).getByText('-45.0%')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Spending chart view'), { target: { value: 'change' } })
  expect(screen.getByRole('img', { name: 'Month-on-month spending change chart · SGD' })).toBeInTheDocument()
})

it('does not substitute partial spending or compare across a missing month', () => {
  const missing = month({ complete: false, spending: { total: null, partial_total: { value: '12', currency: 'SGD' } } as TrendMonthOut['spending'],
    spending_change: null, spending_change_pct: null, spending_change_reason: 'Both months need full coverage.' })
  render(<SpendingComparisonChart currency="SGD" trend={[missing]} />)
  expect(screen.getByRole('status')).toHaveTextContent('Both months need full coverage.')
  expect(screen.queryByText('SGD 12.00')).toBeNull()
  expect(screen.queryByText('0.0%')).toBeNull()
})

it('distinguishes a rise, no change and an undefined percentage from zero', () => {
  expect(spendingChangeLabel(month({ spending_change: { value: '22.50', currency: 'SGD' }, spending_change_pct: '45.0' }), 'SGD')).toContain('rose by SGD 22.50 (45.0%)')
  expect(spendingChangeLabel(month({ spending_change: { value: '0.00', currency: 'SGD' }, spending_change_pct: '0.0' }), 'SGD')).toContain('unchanged')
  expect(spendingChangeLabel(month({ spending_change: { value: '10.00', currency: 'SGD' }, spending_change_pct: null }), 'SGD')).toContain('Previous spending was zero; no percentage comparison.')
})
