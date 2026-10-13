import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { reportedApi, reportedMoney, WorkspaceReportingDetails } from './workspaceReporting'

const apiMock = vi.fn()
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: (...args: unknown[]) => apiMock(...args) }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

const valued = (value: string, date: string, native = '10.00') => ({
  native: { value: native, currency: 'USD' }, reported: { value, currency: 'SGD' },
  purpose: 'flow', basis: 'monthly_proxy', valuation_as_of: date, rate_as_of: null,
  rate_period: date.slice(0, 7), rate_path: [], forward_policy: null, unavailable: null,
})

describe('workspace reporting', () => {
  it('keeps equal native amounts at different dates separate and preserves native submissions', async () => {
    const native = { rows: [{ amount: '10.00', currency: 'USD' }, { amount: '10.00', currency: 'USD' }] }
    apiMock.mockResolvedValue({ native, reporting_currency: 'SGD', generation: 'g1', amounts: [
      { path: ['rows', 0, 'amount'], label: 'January payment', value: valued('12.00', '2026-01-05') },
      { path: ['rows', 1, 'amount'], label: 'February payment', value: valued('14.00', '2026-02-05') },
    ] })
    const data = await reportedApi<typeof native>('debts')
    expect(reportedMoney(data.rows[0], 'amount', 'USD')).toBe('12.00 SGD')
    expect(reportedMoney(data.rows[1], 'amount', 'USD')).toBe('14.00 SGD')
    expect(JSON.parse(JSON.stringify(data))).toEqual(native)
    expect(data.rows[0]).toEqual({ amount: '10.00', currency: 'USD' })
    expect(apiMock).toHaveBeenCalledWith('reporting/workspaces/debts')
    render(<WorkspaceReportingDetails data={data}/>)
    expect(screen.getByText(/January payment/)).toBeInTheDocument()
    expect(screen.getByText('12.00 SGD')).toBeInTheDocument()
  })

  it('shows missing-rate reasons and leaves the original row selectable', async () => {
    const value = { ...valued('12.00', '2026-01-05'), reported: null, basis: null,
      unavailable: { reason: 'RATE_NOT_IMPORTED', message: 'No January rate', remedy: 'Import the January rate.' } }
    apiMock.mockResolvedValue({ native: { rows: [{ amount: '10.00', currency: 'USD' }] }, amounts: [{ path: ['rows', 0, 'amount'], value }] })
    const data = await reportedApi<{rows: {amount: string}[]}>('cards')
    expect(data.rows).toHaveLength(1)
    expect(data.rows[0].amount).toBe('10.00')
    expect(reportedMoney(data.rows[0], 'amount', 'USD')).toContain('No January rate')
    expect(reportedMoney(data.rows[0], 'amount', 'USD')).toContain('Import the January rate.')
    // Unconverted is not unknown: the original amount stays on screen.
    expect(reportedMoney(data.rows[0], 'amount', 'USD')).toContain('10.00 USD not converted')
  })

  it('caps the reporting disclosure so long reads stay usable', async () => {
    const rows = Array.from({ length: 75 }, () => ({ amount: '1.00', currency: 'USD' }))
    apiMock.mockResolvedValue({ native: { rows }, reporting_currency: 'SGD', amounts: rows.map((_, i) => ({ path: ['rows', i, 'amount'], label: `row ${i}`, value: valued('1.20', '2026-01-05') })) })
    const data = await reportedApi<{ rows: typeof rows }>('spending-controls')
    render(<WorkspaceReportingDetails data={data}/>)
    expect(screen.getByText(/Showing the first 60 of 75 amounts/)).toBeInTheDocument()
    expect(screen.getAllByText('1.20 SGD')).toHaveLength(60)
  })

  it('never rounds server-provided currency precision and sends preview inputs unchanged', async () => {
    const body = { amount: '10.123', currency: 'KWD', match: { amount_band: { min: '10.123', currency: 'KWD' } } }
    apiMock.mockResolvedValue({ native: body, amounts: [{ path: ['amount'], value: { ...valued('12.340', '2026-01-05'), reported: { value: '12.340', currency: 'KWD' } } }] })
    const data = await reportedApi<typeof body>('rules/compile', body)
    expect(reportedMoney(data, 'amount', 'KWD')).toBe('12.340 KWD')
    expect(apiMock).toHaveBeenCalledWith('reporting/workspaces/rules/compile', body)
    expect(data.match.amount_band).toEqual(body.match.amount_band)
  })
})
