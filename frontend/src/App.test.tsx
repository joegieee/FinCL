import type React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { moneyText } from '@/test/moneyText'

// jsdom has no layout; let Recharts' ResponsiveContainer resolve to a fixed box.
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return { ...actual, ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
    <div style={{ width: 600, height: 300 }}>{children}</div> }
})

/**
 * Navigate the rail the way a person does: open the group, then the
 * destination inside it. The rail shows six groups and expands one at a time,
 * so a destination is two clicks from cold — the helper keeps that real rather
 * than reaching past the grouping every test would otherwise have to repeat.
 */
async function openTab(name: string, root: HTMLElement | null = null) {
  const { sectionChrome, sectionOfTab } = await import('@/lib/tabs')
  const scope = root ? within(root) : screen
  const section = sectionOfTab[name]
  const group = sectionChrome[section]
  if (group.label !== name) {
    fireEvent.click(await scope.findByRole('button', { name: new RegExp(`^${group.label}`) }))
  }
  fireEvent.click(await scope.findByRole('button', { name: new RegExp(`^${name}`) }))
}

type Json = Record<string, unknown>
const HEALTH: Json = { csrf: 'csrf-token', demo: false, label: 'Personal ledger' }

function dashboard(over: Partial<Json> = {}): Json {
  return {
    generation: 'sha256:abc', freshness: 'current', updated_at: '2026-02-01T00:00:00+00:00',
    start: '2026-01-01', end: '2026-01-31', currency: 'SGD', currencies: ['SGD'],
    covered: true, coverage_reasons: [],
    summary: { spending: '1234.50', income: '4200.00', fees: '0', refunds: '0',
      reimbursements: '0', non_income_inflow: '0', pending_review: '0', net_economic_flow: '2965.50' },
    categories: ['Food & Dining / Groceries', 'Internal / Transfer', 'Unclassified / Unclassified'],
    custom_categories: [], category_totals: [], merchant_totals: [], trend: [], recurring: [],
    plans: [], recent: [], review_count: 0, accounts: ['Revolut'], total_entries: 0, transfer_proposals: [],
    ...over,
  }
}

type Cv = {
  total: { value: string; currency: string } | null
  status: 'available' | 'partial' | 'unavailable'
  partial_total: { value: string; currency: string } | null
  included: unknown[]
  excluded: unknown[]
  excluded_entry_count: number
  unavailable_reasons: string[]
  excluded_detail: unknown[]
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

function moneyFlow(over: Partial<Json> = {}): Json {
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

function entry(over: Partial<Json> = {}): Json {
  return {
    id: 'e1', anchor: 'a1', date: '2026-01-05', precision: 'DAY', amount: '-12.00', fee: '0',
    currency: 'SGD', account: 'Revolut', merchant: 'Cafe Social', description: 'CAFE SOCIAL',
    category: 'Food & Dining / Groceries', basis: 'rule', source_location: 'Lines 1–2',
    bucket: 'spending', eligible: true, review: false, source_family: 'CSV', lifecycle_state: 'COMPLETED',
    ...over,
  }
}

function reportedRow(over: Partial<Json> = {}): Json {
  const { entry: entryOver, ...rest } = over
  return {
    entry: entry((entryOver as Partial<Json>) ?? {}),
    amount: { native: { value: '-12.00', currency: 'SGD' }, reported: null, purpose: 'FLOW', basis: 'identity', valuation_as_of: '2026-01-31', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: null },
    fee: null,
    statement_detail: {
      original: null, settled: { currency: 'SGD', amount_text: '12.00' }, explicit_fee: null,
      statement_cash_movement: null, printed_conversion: null, printed_charge_percent: null, effective_rate: null,
      validation: { status: 'not_checkable', reason: null },
      provenance: { document_record_id: null, source_location: null, detail_basis: 'unavailable', normalizer_version: 'v1' },
    },
    ...rest,
  }
}

function mockFetch(
  overview: () => { status?: number; body: Json },
  transactions: (url: URL) => { status?: number; body: Json } = () => ({ body: { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows: [], total: 0, next_cursor: null } }),
  moneyFlowResponse: () => { status?: number; body: Json } = () => ({ body: moneyFlow() }),
) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const reply = (status: number, body: Json) =>
      ({ ok: status < 400, status, json: async () => body }) as Response
    if (url.includes('/api/v1/health')) return reply(200, HEALTH)
    if (url.includes('/api/v1/overview') || url.includes('/api/v1/reporting/workspaces/overview')) { const r = overview(); return reply(r.status ?? 200, r.body) }
    if (url.includes('/api/v1/reporting-currency')) {
      return reply(200, {
        generation: 'sha256:abc',
        reporting_policy: { currency: 'SGD', minor_unit_scale: 2, journal_seq: 0, decided_at: null, policy_version: 'v1', is_default: true },
        currency_registry: [
          { code: 'SGD', display_name: 'Singapore Dollar', minor_unit_scale: 2 },
          { code: 'USD', display_name: 'US Dollar', minor_unit_scale: 2 },
        ],
        coverage: {},
      })
    }
    if (url.includes('/api/v1/money-flow')) { const r = moneyFlowResponse(); return reply(r.status ?? 200, r.body) }
    if (url.includes('/api/v1/reporting/transactions')) { const r = transactions(new URL(url, 'http://x')); return reply(r.status ?? 200, r.body) }
    if (url.includes('/api/v1/history')) return reply(200, [] as unknown as Json)
    throw new Error(`unmocked request: ${url}`)
  })
}

async function mountApp() {
  vi.resetModules()
  const { default: App } = await import('./App')
  return render(<App />)
}

beforeEach(() => { window.history.replaceState(null, '', '/') })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('App state matrix', () => {
  it('renders the overview from the reported money-flow read', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    // Economic flow, average monthly spending + fees and cash at cutoff all come
    // from one GET /api/v1/money-flow read (M3b.3 step 10) — value-then-currency
    // via the shared <ConsolidatedMoney>.
    expect(await screen.findByText(moneyText('4,200.00 SGD'))).toBeInTheDocument()
    expect(screen.getAllByText(moneyText('1,234.50 SGD')).length).toBeGreaterThan(0)
    expect(screen.getByText(moneyText('5,000.00 SGD'))).toBeInTheDocument()
    expect(screen.getByText(/Reporting in SGD/)).toBeInTheDocument()
  })

  it('shows a coverage warning and never a fabricated zero when a reported card is unavailable', async () => {
    const uncovered = dashboard({ covered: false })
    const unavailable = moneyFlow({
      summary: {
        economic_flow: { inflow: cv(null), outflow: cv(null), net: cv(null), exceptional_adjustments: cv('0') },
        gross_burn: { amount: null, included_months: [], excluded_months: ['2026-01'], month_count: 0, reason: 'No complete covered month.', definition_version: 'gross-burn-v2' },
        cash_at_cutoff: { amount: cv(null), balance_as_of: null, valuation_as_of: '2026-01-31' },
        forecast_closing: { base: cv(null), conservative: cv(null), opening_balance_as_of: null, valuation_as_of: '2026-01-31', horizon_end: null },
      },
    })
    vi.stubGlobal('fetch', mockFetch(() => ({ body: uncovered }), undefined, () => ({ body: unavailable })))
    await mountApp()
    expect(await screen.findByText(/missing coverage/i)).toBeInTheDocument()
    expect(await screen.findAllByText('Not available')).not.toHaveLength(0)
    expect(screen.queryByText(moneyText('0.00 SGD'))).not.toBeInTheDocument()
    expect(screen.queryByText('0.00 SGD')).not.toBeInTheDocument()
  })

  it('surfaces a stale generation banner before editing', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard({ freshness: 'stale' }) })))
    await mountApp()
    expect(await screen.findByText(/New ledger inputs are available/i)).toBeInTheDocument()
  })

  it('shows a recoverable error when the overview request fails', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ status: 500, body: { detail: 'boom' } })))
    await mountApp()
    expect(await screen.findByText(/couldn.t complete that/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})

describe('Review tab', () => {
  it('invites review with a count and both legs of a proposed transfer', async () => {
    const withProposal = dashboard({
      transfer_proposals: [{
        id: 'l1', link_kind: 'SAME_CURRENCY_TRANSFER', method: 'seed-v1',
        left: 'anchor-left', right: 'anchor-right',
        left_account: 'DBS Multi', right_account: 'Revolut',
        left_description: 'PAYNOW TRANSFER', right_description: 'Top-Up',
        left_amount: '100.00', left_currency: 'SGD',
        right_amount: '100.00', right_currency: 'SGD',
        left_date: '2026-01-10', right_date: '2026-01-10',
        basis: 'Matched by description and date across accounts',
      }],
    })
    vi.stubGlobal('fetch', mockFetch(() => ({ body: withProposal })))
    await mountApp()
    await openTab('Review')
    expect(await screen.findByText(/awaiting a decision/i)).toBeInTheDocument()
    expect(screen.getByText('DBS Multi')).toBeInTheDocument()
    expect(screen.getByText('Revolut')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Confirm transfer/i })).toBeInTheDocument()
  })

  it('shows an explicit empty state when there is nothing to review', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await openTab('Review')
    expect(await screen.findByText(/Nothing to review/i)).toBeInTheDocument()
  })
})

describe('Scope control follows tab metadata', () => {
  it('keeps Categorization independent of the global period and currency', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await openTab('Categorization')
    await screen.findByRole('heading', { name: 'Categorization' })
    expect(screen.queryByRole('group', { name: 'Period presets' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Display currency')).not.toBeInTheDocument()
  })

  it('keeps the all-time Categorization inbox free of a global period caption', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await openTab('Categorization')
    await screen.findByRole('heading', { name: 'Categorization' })
    expect(screen.queryByRole('group', { name: 'Period presets' })).not.toBeInTheDocument()
    expect(screen.queryByText(/transfers excluded from spending/)).not.toBeInTheDocument()
  })

  it('shows the period picker but no global currency select on the migrated Overview tab, with every pocket represented', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('heading', { name: 'Overview' })
    expect(await screen.findByRole('group', { name: 'Period presets' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Display currency')).not.toBeInTheDocument()
  })

  it('keeps a fresh visit from writing a pocket filter into the URL', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('heading', { name: 'Overview' })
    await screen.findByRole('group', { name: 'Period presets' })
    expect(new URLSearchParams(location.search).has('currency')).toBe(false)
  })

  it('keeps an explicit ?currency= in the URL across a reload', async () => {
    window.history.replaceState(null, '', '/?currency=USD')
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('group', { name: 'Period presets' })
    expect(new URLSearchParams(location.search).get('currency')).toBe('USD')
  })

  it('hides the scope control entirely on a scope: none tab', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await openTab('Debts')
    await screen.findByRole('heading', { name: 'Debts' })
    expect(screen.queryByRole('group', { name: 'Period presets' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Display currency')).not.toBeInTheDocument()
  })

  it('uses a local snapshot source currency instead of a global Variance filter', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await openTab('Variance')
    await screen.findByLabelText('Snapshot source currency')
    expect(screen.queryByLabelText('Display currency')).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Period presets' })).not.toBeInTheDocument()
  })

  // M4 step 16 (Release B) audit: Cash, Net worth, Forecast and Income are
  // all migrated `scope: 'none'` tabs (plan §7 M4, §6.3 "no migrated screen
  // is still being narrowed by a legacy tab's local currency selector").
  // Each owns its own explicit, locally-scoped filter/caption instead.
  it.each(['Cash', 'Net worth', 'Forecast', 'Income'])(
    'hides the legacy global currency select on the migrated %s tab', async (tabName) => {
      vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
      await mountApp()
      await openTab(tabName)
      // The tab's own reported fetch is unmocked and rejects; either its
      // error state or its content settles, but the shared scope-bar chrome
      // (computed from tab metadata, independent of the tab body) never
      // shows the legacy selector regardless of which one renders. Waiting
      // for the tab's own error alert (every migrated view's generic catch
      // -all path) confirms the fetch attempt has actually settled.
      await screen.findByRole('alert')
      expect(screen.queryByRole('group', { name: 'Period presets' })).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Display currency')).not.toBeInTheDocument()
    },
  )
})

describe('Navigation rail', () => {
  it('puts the skip link first in tab order, ahead of the rail', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('navigation', { name: 'Main navigation' })
    const skipLink = screen.getByRole('link', { name: 'Skip to content' })
    expect(document.body.querySelector('a, button')).toBe(skipLink)
    expect(skipLink).toHaveAttribute('href', '#main-content')
  })

  it('gives the rail a single tab stop that moves with arrow keys', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    const nav = await screen.findByRole('navigation', { name: 'Main navigation' })
    // Open a group so there are sibling destinations to move between; the
    // arrow keys deliberately walk only what is on screen.
    fireEvent.click(within(nav).getByRole('button', { name: /^Money/ }))
    const buttons = Array.from(nav.querySelectorAll('ul button')) as HTMLButtonElement[]
    expect(buttons.filter(b => b.tabIndex === 0)).toHaveLength(1)
    buttons[0].focus()
    fireEvent.keyDown(buttons[0], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(buttons[1])
  })

  it('opens every destination from the command palette on the keyboard shortcut', async () => {
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('heading', { name: 'Overview' })
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true })
    const input = await screen.findByLabelText('Search destinations')
    fireEvent.change(input, { target: { value: 'Money Owed' } })
    fireEvent.click(await screen.findByRole('option', { name: /Money Owed/ }))
    await screen.findByRole('heading', { name: 'Obligations' })
    expect(screen.queryByLabelText('Jump to a destination')).not.toBeInTheDocument()
  })

  it('swaps the sidebar for a top bar, section bar and page tabs on a small screen, keeping its tools', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }))
    vi.stubGlobal('fetch', mockFetch(() => ({ body: dashboard() })))
    await mountApp()
    await screen.findByRole('heading', { name: 'Overview' })
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    for (const name of ['Go anywhere', 'History', 'Theme: system', 'Refresh']) expect(within(screen.getByRole('banner')).getByRole('button', { name })).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Sections' })).getByRole('button', { name: 'Commitments' }))
    fireEvent.click(within(await screen.findByRole('navigation', { name: 'Commitments pages' })).getByRole('button', { name: 'Debts' }))
    await screen.findByRole('heading', { name: 'Debts' })
  })
})

describe('Transactions tab pagination', () => {
  it('requests page one from the reported endpoint and pages forward with a cursor, with every pocket shown by default', async () => {
    const pageOne = { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows: [reportedRow({ entry: { id: 'e1', anchor: 'a1', merchant: 'Cafe Social' } })], total: 2, next_cursor: 'CURSOR2' }
    const pageTwo = { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows: [reportedRow({ entry: { id: 'e2', anchor: 'a2', merchant: 'Bookshop' } })], total: 2, next_cursor: null }
    const seen: string[] = []
    const sawPocketCurrency: boolean[] = []
    vi.stubGlobal('fetch', mockFetch(
      () => ({ body: dashboard() }),
      (url) => {
        const cursor = url.searchParams.get('cursor')
        seen.push(cursor ?? 'none')
        sawPocketCurrency.push(url.searchParams.has('pocket_currency'))
        return { body: cursor === 'CURSOR2' ? pageTwo : pageOne }
      },
    ))
    await mountApp()
    await openTab('Transactions')

    expect(await screen.findByText('Cafe Social')).toBeInTheDocument()
    expect(sawPocketCurrency.every(v => v === false)).toBe(true)
    expect(screen.getByText(/Showing 1 of 2 · page 1/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    expect(await screen.findByText('Bookshop')).toBeInTheDocument()
    expect(screen.getByText(/Showing 1 of 2 · page 2/)).toBeInTheDocument()
    expect(seen).toEqual(['none', 'CURSOR2'])

    fireEvent.click(screen.getByRole('button', { name: /Previous/ }))
    expect(await screen.findByText('Cafe Social')).toBeInTheDocument()
  })

  it('sends the unresolved-only role filter and selects a row with the keyboard', async () => {
    const seen: string[] = []
    vi.stubGlobal('fetch', mockFetch(
      () => ({ body: dashboard() }),
      (url) => {
        seen.push(url.searchParams.get('unresolved_only') ?? 'none')
        return { body: { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows: [reportedRow({ entry: { id: 'e1', anchor: 'a1', merchant: 'Cafe Social', cost_behavior: 'unresolved', need: 'unresolved' } })], total: 1, next_cursor: null } }
      },
    ))
    await mountApp()
    await openTab('Transactions')
    expect(await screen.findByText('Cafe Social')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Unresolved only'))
    await vi.waitFor(() => expect(seen).toContain('true'))

    const row = (await screen.findByText('Cafe Social')).closest('tr') as HTMLElement
    row.focus()
    fireEvent.keyDown(row, { key: ' ' })
    expect(await screen.findByRole('region', { name: 'Bulk assignment' })).toBeInTheDocument()
  })

  it('leaves an amount-unavailable row visible and selectable, with its native evidence shown', async () => {
    const unpriceable = reportedRow({ entry: { id: 'e1', anchor: 'a1', merchant: 'Cafe Social' } })
    unpriceable.amount = { native: { value: '-12.00', currency: 'SGD' }, reported: null, purpose: 'FLOW', basis: null, valuation_as_of: '2026-01-31', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: { reason: 'RATE_NOT_IMPORTED', message: 'No rate available', remedy: 'Import a rate.' } }
    vi.stubGlobal('fetch', mockFetch(
      () => ({ body: dashboard() }),
      () => ({ body: { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows: [unpriceable], total: 1, next_cursor: null } }),
    ))
    await mountApp()
    await openTab('Transactions')
    expect(await screen.findByText('Cafe Social')).toBeInTheDocument()
    expect(screen.getByText('No exchange rate has been imported for this date.')).toBeInTheDocument()
    expect(screen.getByText(/Native:/)).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select Cafe Social'))
    expect(await screen.findByRole('region', { name: 'Bulk assignment' })).toBeInTheDocument()
  })
})
