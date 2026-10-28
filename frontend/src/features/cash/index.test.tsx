import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadReportedCash = vi.fn()
const loadReportingPolicy = vi.fn()
const recordBalanceObservation = vi.fn()
const retireBalanceObservation = vi.fn()
const loadCoverageReport = vi.fn()
const loadCertifications = vi.fn()
const certifyCoverage = vi.fn()
const retireCoverage = vi.fn()
vi.mock('./api', () => ({
  recordBalanceObservation: (...a: unknown[]) => recordBalanceObservation(...a),
  retireBalanceObservation: (...a: unknown[]) => retireBalanceObservation(...a),
  loadCoverageReport: (...a: unknown[]) => loadCoverageReport(...a),
  loadCertifications: (...a: unknown[]) => loadCertifications(...a),
  certifyCoverage: (...a: unknown[]) => certifyCoverage(...a),
  retireCoverage: (...a: unknown[]) => retireCoverage(...a),
  certificationBases: [
    { value: 'account-app-transaction-list', label: "I checked the account's own transaction list" },
    { value: 'statement-inspected-no-activity', label: 'I inspected a statement showing no activity' },
    { value: 'account-closed-or-dormant', label: 'The account was closed or dormant' },
  ],
}))
vi.mock('./reportedApi', () => ({
  emptyCashFilters: { pocketCurrency: '' },
  cashScopeKey: (o: unknown) => JSON.stringify(o),
  loadReportedCash: (...a: unknown[]) => loadReportedCash(...a),
  loadReportingPolicy: (...a: unknown[]) => loadReportingPolicy(...a),
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { CashView } = await import('./index')

function valuedMoney(over: Record<string, unknown> = {}) {
  return {
    native: { value: '360.00', currency: 'SGD' },
    reported: { value: '360.00', currency: 'SGD' },
    purpose: 'position', basis: 'identity', valuation_as_of: '2026-03-05',
    rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null,
    unavailable: null,
    ...over,
  }
}

function pocket(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', institution: 'Revolut', display_name: 'Revolut Current', source_type: 'REVOLUT_CSV',
    currency_pocket: 'SGD', liquid: true, excluded_reason: null, basis: 'statement-running-balance',
    balance: '360.00', as_of: '2026-03-05', date_precision: 'DAY', stale: false, staleness_days: 7,
    unavailable_reason: null, statement_running_balance: null, manual_observation: null,
    derived_balance: '360.00', derived_as_of: '2026-03-05', derived_basis: 'actual-movement-bridge', coverage_status: 'supported',
    reported_balance: valuedMoney(), reported_derived_balance: valuedMoney(),
    ...over,
  }
}

function bridge(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', currency_pocket: 'SGD', opening: '900.00', opening_as_of: '2025-12-31',
    external_inflows: '0', external_outflows: '540.00', owned_transfer_inflows: '0', owned_transfer_outflows: '0',
    closing: '360.00', closing_as_of: '2026-03-05', calculated_closing: '360.00', reconciled: true,
    movement_count: 3, paired_transfer_link_count: 0, unsupported_reason: null,
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'gen-1', reporting_currency: 'SGD', policy_version: 'v1', as_of: '2026-05-01', pocket_currency: null,
    generated_for: '2026-05-01', stale_after_days: 14, supported_cutoff: '2026-03-05',
    current_position_supported: false,
    current_position_reasons: ['complete actual movements from the supported cutoff through today are unavailable'],
    combined_liquid_position: { SGD: '360.00' }, liquid_total: { SGD: '360.00' },
    liquid_total_withheld: {},
    combined: {
      amount: { total: { value: '360.00', currency: 'SGD' }, status: 'available', partial_total: null, included: [], excluded: [], excluded_entry_count: 0, unavailable_reasons: [], excluded_detail: [], rate_path: [] },
      balance_as_of: '2026-03-05', valuation_as_of: '2026-03-05',
    },
    pockets: [pocket()],
    partial_position: {
      as_of: '2026-03-05', by_currency: { SGD: '360.00' },
      contributing_pockets: [{ account_id: 'acc-1', currency_pocket: 'SGD', display_name: 'Revolut Current', balance: '360.00', basis: 'actual-movement-bridge' }],
      short_pockets: [], is_complete: true, reason: null,
    },
    coverage_summary: {
      included_pockets: 1, covered_pockets: 1, short_pockets: 0, ratio: 1,
      basis: 'share of included liquid pockets with a coverage-backed balance', status: 'complete',
    },
    stale: false, missing_intervals: [],
    cash_bridge: { pockets: [bridge()], combined_by_currency: { SGD: { opening: '900.00', external_inflows: '0', external_outflows: '540.00', closing: '360.00', reconciled: true, owned_transfer_pairs_cancelled: 0 } }, owned_transfer_pairs_cancelled: 0, policy: 'p' },
    ...over,
  }
}

function coverageReport(over: Record<string, unknown> = {}) {
  return {
    generated_for: '2026-05-01',
    coverage: {
      included_pockets: 1, covered_pockets: 1, short_pockets: 0, ratio: 1,
      basis: 'share of included liquid pockets with a coverage-backed balance', status: 'complete',
    },
    supported_cutoff: '2026-03-05', current_position_supported: true, remedies: [],
    ...over,
  }
}

function coverageDefaults() {
  loadCoverageReport.mockResolvedValue(coverageReport())
  loadCertifications.mockResolvedValue({ certifications: [] })
}

beforeEach(() => { coverageDefaults() })
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('CashView', () => {
  it('labels a historical common cutoff and withholds the available-today figure', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText('Not observed as of today')).toBeInTheDocument()
    expect(screen.getByText('Observed through 2026-03-05')).toBeInTheDocument()
    expect(screen.getByText(/complete actual movements from the supported cutoff through today are unavailable/)).toBeInTheDocument()
    expect(screen.queryByText(/available today/i)).toBeNull()
    expect(screen.getByText(/not reported as today’s cash/)).toBeInTheDocument()
    expect(screen.getByText('Reporting in SGD')).toBeInTheDocument()
  })

  it('shows a withheld currency total with its reason and never a zero', async () => {
    loadReportedCash.mockResolvedValue(page({
      supported_cutoff: null,
      combined_liquid_position: {}, liquid_total: {},
      liquid_total_withheld: { SGD: 'withheld: no common supported cutoff' },
      current_position_reasons: ['included liquid pockets do not share a supported balance cutoff'],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText('withheld: no common supported cutoff', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/1 currency withheld/)).toBeInTheDocument()
    expect(screen.queryByText('SGD 0')).toBeNull()
    expect(screen.queryByText('SGD 0.00')).toBeNull()
  })

  it('records a manual balance observation and refreshes the position', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    recordBalanceObservation.mockResolvedValue({ status: 'recorded' })
    const onSaved = vi.fn()
    render(<CashView generation="gen-1" onSaved={onSaved} onOpenSetup={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Record manual balance/ }))
    expect(screen.getByText(/A dated fact asserted directly/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Balance (SGD)'), { target: { value: '0.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'USD sub-pocket is unused' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save observation' }))

    await waitFor(() => expect(recordBalanceObservation).toHaveBeenCalledWith('gen-1', expect.objectContaining({
      account_id: 'acc-1', currency_pocket: 'SGD', balance: '0.00', reason: 'USD sub-pocket is unused',
    })))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(loadReportedCash).toHaveBeenCalledTimes(2)
  })

  it('offers to retire an existing manual observation', async () => {
    loadReportedCash.mockResolvedValue(page({
      pockets: [pocket({ manual_observation: { balance: '0.00', as_of: '2026-03-01', basis: 'manual-observation', observation: 'obs-1' } })],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    retireBalanceObservation.mockResolvedValue({ status: 'recorded' })
    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Update/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Retire this one' }))
    fireEvent.change(screen.getByLabelText('Reason to retire'), { target: { value: 'entered by mistake' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retire observation' }))

    await waitFor(() => expect(retireBalanceObservation).toHaveBeenCalledWith('gen-1', 'obs-1', 'entered by mistake'))
  })

  it('shows an explicit, clearable Account-currency filter that narrows pockets', async () => {
    loadReportedCash.mockResolvedValue(page({
      pockets: [pocket(), pocket({ account_id: 'acc-2', currency_pocket: 'USD', balance: '200.00' })],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    await screen.findByText('Reporting in SGD')
    fireEvent.change(screen.getByLabelText('Filter by account currency'), { target: { value: 'USD' } })

    await waitFor(() => expect(loadReportedCash).toHaveBeenLastCalledWith(
      expect.objectContaining({ filters: { pocketCurrency: 'USD' } }),
    ))
  })

  it('retains the last-good response on a same-scope 409 and clears on first-load stale', async () => {
    const { ApiError } = await import('@/lib/api')
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadReportedCash.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText('Refresh required.')).toBeInTheDocument()

    loadReportedCash.mockResolvedValueOnce(page())
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await screen.findByText('Not observed as of today')

    loadReportedCash.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('Showing the last loaded values for this exact scope.')).toBeInTheDocument()
    expect(screen.getByText('Not observed as of today')).toBeInTheDocument()
  })
})

describe('CashView coverage panel', () => {
  it('reports the proven share of liquid pockets', async () => {
    loadReportedCash.mockResolvedValue(page({
      coverage_summary: {
        included_pockets: 2, covered_pockets: 1, short_pockets: 1, ratio: 0.5,
        basis: 'share of included liquid pockets with a coverage-backed balance', status: 'partial',
      },
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })

    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText('1 of 2 liquid pockets proven')).toBeInTheDocument()
    expect(screen.getByText('50% covered')).toBeInTheDocument()
  })

  it('shows the most current partial figure and names what it left out', async () => {
    loadReportedCash.mockResolvedValue(page({
      coverage_summary: {
        included_pockets: 2, covered_pockets: 1, short_pockets: 1, ratio: 0.5,
        basis: 'share of included liquid pockets with a coverage-backed balance', status: 'partial',
      },
      partial_position: {
        as_of: '2026-05-01', by_currency: { SGD: '900.00' },
        contributing_pockets: [{ account_id: 'acc-1', currency_pocket: 'SGD', display_name: 'Revolut Current', balance: '900.00', basis: 'actual-movement-bridge' }],
        short_pockets: [{
          account_id: 'acc-2', currency_pocket: 'USD', display_name: 'Revolut USD',
          reason: 'complete actual-movement coverage is not established through this date',
          best_supported_cutoff: '2026-03-05',
        }],
        is_complete: false,
        reason: '1 of 2 liquid pockets have no coverage-backed balance at 2026-05-01',
      },
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })

    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText(/Most current figure available/)).toBeInTheDocument()
    expect(screen.getAllByText(/SGD 900.00/).length).toBeGreaterThan(0)
    expect(screen.getByText(/proven only through 2026-03-05/)).toBeInTheDocument()
    // It must never be presented as the whole position.
    expect(screen.getByText(/This is not your total cash/)).toBeInTheDocument()
  })

  it('offers the certification dialog with the exact range the gap names', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadCoverageReport.mockResolvedValue(coverageReport({
      current_position_supported: false,
      coverage: {
        included_pockets: 1, covered_pockets: 0, short_pockets: 1, ratio: 0,
        basis: 'share of included liquid pockets with a coverage-backed balance', status: 'unavailable',
      },
      remedies: [{
        action: 'certify-quiet-interval', decision_kind: 'MOVEMENT_COVERAGE_CERTIFY',
        account_id: 'acc-1', currency_pocket: 'SGD', display_name: 'Revolut Current',
        institution: 'Revolut', unlocks: 'unblocks-shared-cutoff', rank: 0,
        detail: 'Check the account transaction list from 2026-03-05 to 2026-05-01.',
        parameters: { period_start: '2026-03-05', period_end: '2026-05-01' },
      }],
    }))
    certifyCoverage.mockResolvedValue({ status: 'committed' })

    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Certify range/ }))
    const from = await screen.findByLabelText('No movement from')
    expect(from).toHaveValue('2026-03-05')
    expect(screen.getByLabelText('Through')).toHaveValue('2026-05-01')

    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'app shows no activity' } })
    fireEvent.click(screen.getByRole('button', { name: 'Certify this range' }))

    await waitFor(() => expect(certifyCoverage).toHaveBeenCalledWith('gen-1', {
      account_id: 'acc-1', currency_pocket: 'SGD',
      period_start: '2026-03-05', period_end: '2026-05-01',
      basis: 'account-app-transaction-list', reason: 'app shows no activity',
    }))
  })

  it('opens Setup from the import remedy', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadCoverageReport.mockResolvedValue(coverageReport({
      remedies: [{
        action: 'import-statement', decision_kind: null,
        account_id: 'acc-1', currency_pocket: 'SGD', display_name: 'Revolut Current',
        institution: 'Revolut', unlocks: 'unblocks-shared-cutoff', rank: 0,
        detail: 'This pocket has no balance boundary at all.', parameters: {},
      }],
    }))
    const onOpenSetup = vi.fn()

    render(<CashView generation="gen-1" onOpenSetup={onOpenSetup} />)
    fireEvent.click(await screen.findByRole('button', { name: /Import a statement/ }))

    expect(onOpenSetup).toHaveBeenCalledOnce()
  })

  it('lists an active certification and can retire it', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadCertifications.mockResolvedValue({
      certifications: [{
        certification: 'cert-1', account_id: 'acc-1', currency_pocket: 'SGD',
        display_name: 'Revolut Current', institution: 'Revolut',
        period_start: '2026-03-06', period_end: '2026-05-01',
        basis: 'account-app-transaction-list',
      }],
    })
    retireCoverage.mockResolvedValue({ status: 'committed' })

    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText(/2026-03-06 → 2026-05-01/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Retire the certification/ }))

    await waitFor(() => expect(retireCoverage).toHaveBeenCalledWith(
      'gen-1', 'cert-1', 'no longer certain this range was quiet',
    ))
  })

  it('says nothing is outstanding when every pocket is proven', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })

    render(<CashView generation="gen-1" onOpenSetup={() => {}} />)

    expect(await screen.findByText('Every liquid pocket is proven')).toBeInTheDocument()
    expect(screen.getByText(/Nothing is outstanding/)).toBeInTheDocument()
  })
})
