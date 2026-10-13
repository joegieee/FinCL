import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReportsView } from './index'

const apiMock = vi.fn()
vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: (...args: unknown[]) => apiMock(...args) }))
afterEach(() => { cleanup(); apiMock.mockReset() })

const saved = {
  report_id: '22222222-2222-4222-8222-222222222222', name: 'Monthly food', reason: 'r', lens: 'recorded_flow',
  scope: { window: { kind: 'recent_months', months: 3 }, currencies: [], accounts: [], categories: ['Food / Groceries'], trips: [], countries: [] },
  group_by: ['month'], columns: ['gross_cost', 'entry_count'],
}
const result = {
  generation: 'g1', evaluated_on: '2026-10-13', lens: 'recorded_flow', lens_note: 'Recorded lens.',
  report: saved, window: { start: '2026-08-01', end: '2026-10-13' }, group_labels: { month: 'Month' },
  warnings: ['Category “Food / Old” no longer exists, so nothing matches it.'],
  rows: [{ group: [{ key: 'month', value: '2026-09', label: '2026-09' }], currency: 'SGD', gross_cost: '120.00', refunds: '0.00', net_cost: '120.00', entry_count: 4, cost_parts: [] }],
  totals_by_currency: { SGD: { gross_cost: '120.00', refunds: '0.00', net_cost: '120.00', entry_count: 4 } },
}

function routes(path: string, body?: unknown) {
  if (path === 'reports') return Promise.resolve({ reports: [saved] })
  if (path === `reporting/workspaces/reports/${saved.report_id}/result`) return Promise.resolve(result)
  if (path === 'reporting/workspaces/reports/preview') return Promise.resolve({ ...result, warnings: [], report: body })
  if (path === 'trips') return Promise.resolve({ trips: [] })
  if (path === 'preview' || path === 'commands') return Promise.resolve({ status: 'applied', body })
  return Promise.reject(new Error(`unexpected ${path}`))
}

const props = { generation: 'g1', categories: ['Food / Groceries', 'Travel / Hotels'], accounts: ['acct-1'], currencies: ['SGD', 'USD'] }

describe('ReportsView', () => {
  it('opens a saved report, showing current figures, their date and any warning', async () => {
    apiMock.mockImplementation(routes)
    render(<ReportsView {...props} onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /Monthly food/ }))
    expect(await screen.findByRole('cell', { name: '2026-09' })).toBeInTheDocument()
    expect(screen.getByText(/figures as of 13 Oct 2026/)).toBeInTheDocument()
    expect(screen.getByText(/no longer exists/)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Transactions' })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Refunds' })).toBeNull()
  })

  it('shows a new report’s result before saving, then saves only its definition', async () => {
    apiMock.mockImplementation(routes)
    const onSaved = vi.fn()
    render(<ReportsView {...props} onSaved={onSaved}/>)
    fireEvent.click(await screen.findByRole('button', { name: /New report/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Trips by country' } })
    fireEvent.click(screen.getByLabelText('Category'))
    fireEvent.click(screen.getByLabelText('Country'))
    fireEvent.click(screen.getByRole('button', { name: 'Show result' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('reporting/workspaces/reports/preview', expect.anything()))
    const shown = apiMock.mock.calls.find(([path]) => path === 'reporting/workspaces/reports/preview')![1] as Record<string, unknown>
    expect(shown.group_by).toEqual(['country'])
    expect(shown).not.toHaveProperty('reason')

    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Travel spending' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save report' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    const command = apiMock.mock.calls.find(([path]) => path === 'commands')![1] as { kind: string; payload: Record<string, unknown> }
    expect(command.kind).toBe('REPORT_DEFINITION_SET')
    expect(command.payload).toMatchObject({ name: 'Trips by country', lens: 'recorded_flow', group_by: ['country'], reason: 'Travel spending',
      scope: { window: { kind: 'recent_months', months: 3 }, categories: [], countries: [] } })
  })

  it('allows at most two grouping keys', async () => {
    apiMock.mockImplementation(routes)
    render(<ReportsView {...props} onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /New report/ }))
    fireEvent.click(screen.getByLabelText('Month'))
    expect(screen.getByLabelText('Merchant')).toBeDisabled()
  })

  it('deletes a report through preview, then confirm', async () => {
    apiMock.mockImplementation(routes)
    render(<ReportsView {...props} onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /Monthly food/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    fireEvent.change(screen.getByLabelText('Why delete it?'), { target: { value: 'Not needed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview delete' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete report' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('commands', expect.objectContaining({ kind: 'REPORT_DEFINITION_RETIRE' })))
  })
})
