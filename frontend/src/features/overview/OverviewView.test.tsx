import type React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError } from '@/lib/api'
import type { Dashboard } from '@/lib/api'
import { moneyText } from '@/test/moneyText'

type Cv = {
  total: { value: string; currency: string } | null
  status: 'available' | 'partial' | 'unavailable'
  partial_total: { value: string; currency: string } | null
  included: unknown[]
  excluded: unknown[]
  excluded_entry_count: number
  unavailable_reasons: string[]
  excluded_detail: { currency: string; period: string | null; entry_count: number; reasons: string[]; remedies: string[] }[]
  rate_path: unknown[]
}

function cv(total: string | null, over: Partial<Cv> = {}): Cv {
  return {
    total: total !== null ? { value: total, currency: 'SGD' } : null,
    status: total !== null ? 'available' : 'unavailable',
    partial_total: null,
    included: [], excluded: [], excluded_entry_count: 0,
    unavailable_reasons: total !== null ? [] : ['RATE_NOT_IMPORTED'],
    excluded_detail: [], rate_path: [],
    ...over,
  }
}

function moneyFlowPage(over: Record<string, unknown> = {}) {
  return {
    generation: 'sha256:abc', freshness: 'current', reporting_currency: 'SGD', policy_version: 'v1',
    as_of: '2026-01-31', history_start: '2026-01-01', history_end: '2026-01-31', pocket_currency: null,
    summary: {
      economic_flow: { inflow: cv('4200.00'), outflow: cv('1234.50'), net: cv('2965.50'), exceptional_adjustments: cv('0') },
      gross_burn: { amount: cv('1234.50'), included_months: ['2026-01'], excluded_months: [], month_count: 1, reason: null, definition_version: 'gross-burn-v2' },
      cash_at_cutoff: { amount: cv('5000.00'), balance_as_of: '2026-01-31', valuation_as_of: '2026-01-31' },
      forecast_closing: { base: cv('4000.00'), conservative: cv('3500.00'), opening_balance_as_of: '2026-01-31', valuation_as_of: '2026-01-31', horizon_end: '2026-07-31' },
    },
    cutoff: { supported_cash_cutoff: '2026-01-31', first_modelled_day: '2026-02-01', stale_days: 0 },
    limitations: [],
    provenance: { generation_manifest_digest: 'sha256:abc', rate_inventory_digest: 'sha256:def', policy_versions: {} },
    ...over,
  }
}

const reportingPolicy = {
  reporting_policy: { currency: 'SGD', is_default: true, policy_version: null },
  currency_registry: [
    { code: 'SGD', display_name: 'Singapore Dollar', minor_unit_scale: 2 },
    { code: 'USD', display_name: 'US Dollar', minor_unit_scale: 2 },
  ],
}

function dashboard(): Dashboard {
  return { generation: 'sha256:abc' } as Dashboard
}

async function defaultImpl(path: string) {
  if (path.startsWith('money-flow?')) return moneyFlowPage()
  if (path === 'reporting-currency') return reportingPolicy
  throw new Error(`unmocked ${path}`)
}
const apiMock = vi.fn(defaultImpl)
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

// The setup checklist and position glance (`OverviewActionPanel`) call several
// unrelated native readers; stub them so this file exercises only the
// money-flow seam step 10 actually changed.
vi.mock('@/features/baseline/api', () => ({ loadBaselineEstimate: async () => ({ available: false, reason: 'x', by_currency: {} }), loadBaselineReadiness: async () => ({ pockets: [], tasks: [] }) }))
vi.mock('@/features/cash/api', () => ({ loadCashPosition: async () => ({ current_position_supported: false, current_position_reasons: [], liquid_total: {}, generated_for: '2026-01-31' }) }))
vi.mock('@/features/forecast/api', () => ({ loadForecast: async () => ({ available: false, reason: 'x', by_currency: {} }) }))
vi.mock('@/features/networth/api', () => ({ loadNetWorth: async () => ({ per_currency: {} }) }))
vi.mock('@/features/obligations/api', () => ({ loadObligations: async () => ({ obligations: [], totals_by_currency: {} }) }))
vi.mock('@/features/planning/api', () => ({ loadRecurring: async () => ({ candidates: [] }) }))
vi.mock('@/features/setup/api', () => ({ loadImports: async () => ({ imports: [] }) }))

async function mount(props: Partial<{ start: string; end: string; initialPocketCurrency: string; onOpen: (d: string) => void }> = {}) {
  const { OverviewView } = await import('./index')
  const onOpen = props.onOpen ?? (() => {})
  return render(
    <OverviewView
      data={dashboard()} start="2026-01-01" end="2026-01-31" initialPocketCurrency=""
      onOpen={onOpen as never} {...props}
    />,
  )
}

afterEach(() => { cleanup(); apiMock.mockReset(); apiMock.mockImplementation(defaultImpl) })

describe('OverviewView', () => {
  it('shows the reported figures over all pockets by default, with no hidden pocket parameter', async () => {
    await mount()
    expect(await screen.findByText(moneyText('4,200.00 SGD'))).toBeInTheDocument()
    expect(screen.getAllByText(moneyText('1,234.50 SGD')).length).toBeGreaterThan(0)
    expect(screen.getByText(moneyText('5,000.00 SGD'))).toBeInTheDocument()
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('money-flow?'), undefined)
    expect(apiMock.mock.calls.some(([path]) => String(path).includes('pocket_currency'))).toBe(false)
  })

  it('shows reporting currency as a setting caption, not a record filter', async () => {
    await mount()
    await screen.findByText(moneyText('4,200.00 SGD'))
    expect(screen.getByText(/Reporting in SGD/)).toBeInTheDocument()
  })

  it('seeds an explicit, clearable Account-currency filter from a legacy ?currency= value', async () => {
    await mount({ initialPocketCurrency: 'USD' })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('pocket_currency=USD'), undefined))
    expect(screen.getByText('Show all pockets')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Show all pockets'))
    await waitFor(() => expect(apiMock).toHaveBeenLastCalledWith(expect.not.stringContaining('pocket_currency'), undefined))
  })

  it('navigates to a destination workspace through a plain link', async () => {
    const onOpen = vi.fn()
    await mount({ onOpen })
    await screen.findByText(moneyText('4,200.00 SGD'))
    fireEvent.click(screen.getByRole('button', { name: /^Cash on/ }))
    expect(onOpen).toHaveBeenCalledWith('Cash')
  })

  it('folds limitations behind a count and never shows a raw record id', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('money-flow?')) return moneyFlowPage({ limitations: ['debt:4d95af3b-0c6b-50c5-b6c5-9a99361cdd60 has no safe settlement date', 'Rates are missing'] })
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    const summary = await screen.findByText('2 limits on these figures')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText('A debt has no safe settlement date')).toBeInTheDocument()
  })

  it('refetches the summary when the requested period changes', async () => {
    const { rerender } = await mount()
    await screen.findByText(moneyText('4,200.00 SGD'))
    apiMock.mockClear()
    const { OverviewView } = await import('./index')
    rerender(<OverviewView data={dashboard()} start="2026-02-01" end="2026-02-28" initialPocketCurrency="" onOpen={() => {}} />)
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('history_start=2026-02-01'), undefined))
  })

  it('shows the saved same-scope response with a stale banner on a 409, not another scope', async () => {
    let call = 0
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('money-flow?')) {
        call += 1
        if (call === 1) return moneyFlowPage()
        throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findByText(moneyText('4,200.00 SGD'))
    fireEvent.click(screen.getAllByRole('button', { name: /refresh/i })[0])
    await waitFor(() => expect(screen.getByText(/last loaded values for this exact period/)).toBeInTheDocument())
    expect(screen.getAllByText(moneyText('4,200.00 SGD')).length).toBeGreaterThan(0)
  })

  it('shows Refresh required with no numbers on a first-load stale response', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('money-flow?')) throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await waitFor(() => expect(screen.getByText('Refresh required.')).toBeInTheDocument())
    expect(screen.queryByText(moneyText('4,200.00 SGD'))).not.toBeInTheDocument()
  })

  it('clears the last-good response on a reporting-policy change instead of reusing it', async () => {
    let call = 0
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') {
        return call <= 1 ? reportingPolicy : { reporting_policy: { currency: 'USD', is_default: false, policy_version: 'v2' } }
      }
      if (path.startsWith('money-flow?')) {
        call += 1
        if (call === 1) return moneyFlowPage()
        throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findByText(moneyText('4,200.00 SGD'))
    fireEvent.click(screen.getAllByRole('button', { name: /refresh/i })[0])
    await waitFor(() => expect(screen.getByText('Refresh required.')).toBeInTheDocument())
    expect(screen.queryByText(moneyText('4,200.00 SGD'))).not.toBeInTheDocument()
  })

  it('changes the reporting currency through the existing preview/command path, without an automatic refresh', async () => {
    apiMock.mockImplementation(async (path: string, body?: unknown) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('money-flow?')) return moneyFlowPage()
      if (path === 'preview') return { ok: true }
      if (path === 'commands') {
        expect((body as { kind: string }).kind).toBe('REPORTING_CURRENCY_SET')
        return { status: 'recorded_refresh_pending' }
      }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findByText(moneyText('4,200.00 SGD'))
    fireEvent.click(screen.getByRole('button', { name: 'Change' }))
    await waitFor(() => expect(screen.getByLabelText('Reporting currency')).toHaveTextContent('US Dollar'))
    fireEvent.change(screen.getByLabelText('Reporting currency'), { target: { value: 'USD' } })
    fireEvent.change(screen.getByPlaceholderText(/Why change/), { target: { value: 'Relocated to the US' } })
    const form = screen.getByLabelText('Reporting currency').closest('form') as HTMLFormElement
    fireEvent.submit(form)
    await screen.findByRole('button', { name: 'Save change' })
    fireEvent.submit(form)
    await screen.findByText(/Use Refresh before reading updated totals/)
    // Saving never itself calls the ledger refresh command.
    expect(apiMock).not.toHaveBeenCalledWith('refresh', expect.anything())
  })
})
