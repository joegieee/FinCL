import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { NetWorthCharts } from './NetWorthCharts'
import type { NetWorthPageOut } from './reportedApi'

afterEach(cleanup)

it('leaves incomplete valuations unavailable and warns when dated observations change scope', () => {
  const cell = (value: string | null) => ({ total: value === null ? null : { value, currency: 'SGD' }, partial_total: { value: '99', currency: 'SGD' }, unavailable_reasons: [] })
  const page = { reporting_currency: 'SGD', as_of: '2026-10-17',
    totals: { assets: cell('200'), liabilities: cell(null), net_worth: cell('-40') },
    trend: { prior_as_of: '2026-09-17', current_as_of: '2026-10-17', prior_net_worth: cell('10'), current_net_worth: cell('-40'), scope_changed: true },
  } as unknown as NetWorthPageOut
  render(<NetWorthCharts page={page} />)
  const row = screen.getByText('Liabilities').closest('tr')!
  expect(within(row).getByText('Unavailable')).toBeInTheDocument()
  expect(screen.queryByText(/99/)).toBeNull()
  expect(screen.getByText(/Scope changed: these observations contain different components/)).toBeInTheDocument()
  expect(screen.getAllByText(/−40\.00 SGD/)).toHaveLength(2)
})
