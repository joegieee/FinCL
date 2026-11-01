import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError } from '@/lib/api'
import { TransactionsView } from './index'
import { moneyText } from '@/test/moneyText'

function nativeEntry(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'e1', anchor: 'a1', date: '2026-02-01', precision: 'DAY', amount: '-100.00', fee: '0',
    currency: 'SGD', account: 'Revolut', merchant: 'Foo', description: 'FOO', category: 'Food / Dining',
    basis: 'rule', source_location: 'p1', bucket: 'spending', eligible: true, review: false,
    source_family: 'REVOLUT_PDF', lifecycle_state: 'COMPLETED', cost_behavior: 'irregular',
    cost_behavior_basis: 'manual', need: 'discretionary', need_basis: 'manual', role_review_state: 'none',
    suggested_classification: '', ...over,
  }
}

function reportedRow(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    entry: nativeEntry((over.entry as Record<string, unknown>) ?? {}),
    amount: { native: { value: '-100.00', currency: 'SGD' }, reported: null, purpose: 'FLOW', basis: 'identity', valuation_as_of: '2026-02-01', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: null },
    fee: null,
    statement_detail: {
      original: null, settled: { currency: 'SGD', amount_text: '100.00' }, explicit_fee: null,
      statement_cash_movement: null, printed_conversion: null, printed_charge_percent: null, effective_rate: null,
      validation: { status: 'not_checkable', reason: null },
      provenance: { document_record_id: null, source_location: null, detail_basis: 'unavailable', normalizer_version: 'v1' },
    },
    ...over,
  }
}

function page(rows: unknown[], over: Record<string, unknown> = {}): Record<string, unknown> {
  return { generation: 'sha256:abc', reporting_currency: 'SGD', sort: 'date', rows, total: rows.length, next_cursor: null, ...over }
}

const policy = { reporting_policy: { currency: 'SGD', is_default: true, policy_version: 'v1' } }

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string) => apiMock(path) }
})

function mount(props: Partial<Parameters<typeof TransactionsView>[0]> = {}) {
  return render(
    <TransactionsView
      start="2026-02-01" end="2026-02-28" generation="sha256:abc" freshness="current"
      categories={['Food / Dining']} accounts={['Revolut']} currencies={['SGD', 'USD']}
      initialPocketCurrency="" initialCategory="" initialSearch="" initialKind=""
      onOpenEntry={() => {}} onSaved={async () => {}}
      {...props}
    />,
  )
}

afterEach(() => { cleanup(); apiMock.mockReset() })

describe('TransactionsView', () => {
  it('shows every eligible pocket by default, with no pocket_currency parameter sent', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) {
        expect(path).not.toContain('pocket_currency')
        return page([reportedRow({ entry: nativeEntry({ merchant: 'Foo', currency: 'SGD' }) }), reportedRow({ entry: nativeEntry({ id: 'e2', anchor: 'a2', merchant: 'Bar', currency: 'USD' }) })])
      }
      throw new Error(`unmocked ${path}`)
    })
    mount()
    expect(await screen.findByText('Foo')).toBeInTheDocument()
    expect(screen.getByText('Bar')).toBeInTheDocument()
  })

  it('narrows to an explicit Account-currency filter, then clears it back to all pockets', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) return page([reportedRow()])
      throw new Error(`unmocked ${path}`)
    })
    mount()
    await screen.findByText('Foo')
    fireEvent.change(screen.getByLabelText('Account currency filter'), { target: { value: 'USD' } })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('pocket_currency=USD')))
    fireEvent.change(screen.getByLabelText('Account currency filter'), { target: { value: '' } })
    await waitFor(() => expect(apiMock).toHaveBeenLastCalledWith(expect.not.stringContaining('pocket_currency')))
  })

  it('pages forward and back with a server cursor', async () => {
    const seen: string[] = []
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) {
        const cursor = new URL('http://x/' + path).searchParams.get('cursor')
        seen.push(cursor ?? 'none')
        return cursor === 'CURSOR2'
          ? page([reportedRow({ entry: nativeEntry({ id: 'e2', anchor: 'a2', merchant: 'Bar' }) })], { total: 2, next_cursor: null })
          : page([reportedRow({ entry: nativeEntry({ merchant: 'Foo' }) })], { total: 2, next_cursor: 'CURSOR2' })
      }
      throw new Error(`unmocked ${path}`)
    })
    mount()
    expect(await screen.findByText('Foo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    expect(await screen.findByText('Bar')).toBeInTheDocument()
    expect(seen).toEqual(['none', 'CURSOR2'])
    fireEvent.click(screen.getByRole('button', { name: /Previous/ }))
    expect(await screen.findByText('Foo')).toBeInTheDocument()
  })

  it('reuses the same-scope cached page on a 409 for a page it already fetched, without substituting a different scope', async () => {
    let call = 0
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) {
        call++
        const cursor = new URL('http://x/' + path).searchParams.get('cursor')
        if (call === 1) return page([reportedRow({ entry: nativeEntry({ merchant: 'Foo' }) })], { total: 2, next_cursor: 'CURSOR2' })
        if (call === 2 && cursor === 'CURSOR2') return page([reportedRow({ entry: nativeEntry({ id: 'e2', anchor: 'a2', merchant: 'Bar' }) })], { total: 2, next_cursor: null })
        // Third call: back to page one (cursor=null), same scope as call 1 — simulate a 409.
        throw new ApiError('stale', 409)
      }
      throw new Error(`unmocked ${path}`)
    })
    mount()
    expect(await screen.findByText('Foo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    expect(await screen.findByText('Bar')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Previous/ }))
    expect(await screen.findByText(/Showing the last loaded values for this exact page and filters/)).toBeInTheDocument()
    expect(screen.getByText('Foo')).toBeInTheDocument()
  })

  it('shows refresh-required and clears rows on a 409 with no matching cached scope', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) throw new ApiError('stale', 409)
      throw new Error(`unmocked ${path}`)
    })
    mount()
    expect(await screen.findByText(/Refresh required/)).toBeInTheDocument()
    expect(screen.queryByText('Foo')).not.toBeInTheDocument()
  })

  it('retains an unpriceable row, selectable, with a visible reason and native evidence', async () => {
    const unpriceable = reportedRow({
      amount: { native: { value: '-100.00', currency: 'SGD' }, reported: null, purpose: 'FLOW', basis: null, valuation_as_of: '2026-02-01', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: { reason: 'RATE_NOT_IMPORTED', message: 'No rate', remedy: 'Import one.' } },
    })
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) return page([unpriceable])
      throw new Error(`unmocked ${path}`)
    })
    mount()
    await screen.findByText('Foo')
    expect(screen.getByText('No exchange rate has been imported for this date.')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select Foo'))
    expect(await screen.findByRole('region', { name: 'Bulk assignment' })).toBeInTheDocument()
  })

  it('does not double-convert an identity-valued row (native settlement currency equals reporting currency)', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return policy
      if (path.startsWith('reporting/transactions?')) return page([reportedRow()])
      throw new Error(`unmocked ${path}`)
    })
    mount()
    await screen.findByText('Foo')
    // Identity basis renders the native amount directly with no conversion
    // basis tag and no separate "Native:" evidence line (the amount is priced).
    // Appears twice: the visible amount, and Money's own disclosure panel
    // ("Native amount") — both identical, confirming no second conversion.
    expect(screen.getAllByText(moneyText('−100.00 SGD')).length).toBe(2)
    expect(screen.queryByText('Native currency')).not.toBeInTheDocument()
    expect(screen.queryByText(/^Native:/)).not.toBeInTheDocument()
  })
})
