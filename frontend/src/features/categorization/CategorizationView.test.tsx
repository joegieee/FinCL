import { afterEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status = 422 },
  money: (v: string | null, c = 'SGD') => v == null ? 'Unavailable' : `${v} ${c}`,
}))
vi.mock('@/lib/command', () => ({
  useLedgerCommand: () => ({ busy: false, error: '', previewed: null, attempted: false, reset: vi.fn(), act: vi.fn() }),
  // A minimal fake sufficient for these tests: submit calls the mocked
  // api('batch', ...) directly with the same request shape the real
  // `useBatchCommand` (lib/command.ts, unit-tested separately) sends.
  useBatchCommand: (_generation: string, onSaved: (status: string) => void | Promise<void>) => ({
    busy: false, error: '', submitted: null, invalidate: () => {},
    submit: async (items: unknown[], reason: string) => {
      const result = await apiMock('batch', { request_id: 'test-request', expected_generation: _generation, reason, items })
      await onSaved((result as { status: string }).status)
    },
    retry: async () => {},
  }),
}))
vi.mock('@/features/rules/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/features/rules/api')>()
  return { ...actual, compileRule: vi.fn() }
})

const { CategorizationView } = await import('./CategorizationView')

afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear() })

function twoRecordCandidate() {
  return {
    candidate: { label: 'CAFE SOCIAL' },
    records: [
      { anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL #1' },
      { anchor: 'a2', account: 'acct', currency: 'SGD', amount: '-5.00', date: '2026-01-05', direction: 'OUT', active: true, description: 'CAFE SOCIAL #2' },
    ],
  }
}

/** Picks the category from the combobox dropdown -- the same click sequence
 * every test uses once a category name is typed and the option appears. */
function pickCategory(name: string) {
  fireEvent.click(screen.getByText(name, { selector: 'button *' }).closest('button')!)
}

describe('CategorizationView', () => {
  it('shows coverage and opens a candidate into the split review panel without silently choosing a category', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: { rule_assigned: 2, unclassified_active: 4 }, active_categorization_denominator: 6, progress_numerator: 2, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 4, active_count: 4, date_range: { first: '2026-01-01', last: '2026-01-04' }, accounts: ['acct'], directions: { OUT: 4 }, examples: ['CAFE SOCIAL'] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    expect(await screen.findByRole('button', { name: /CAFE SOCIAL/ })).toBeInTheDocument()
    expect(screen.getByText('33%')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /This one/ })
    expect(screen.getByRole('region', { name: 'Review CAFE SOCIAL' })).toBeInTheDocument()
    expect((screen.getByRole('combobox', { name: 'Category' }) as HTMLInputElement).value).toBe('')
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('categorization/derive-rule', { generation: 'g1', anchors: ['a1'] }))
  })

  it('offers a one-click rule suggestion from repeated manual overrides, and applies it (Step 6c)', async () => {
    let suggestionApplied = false
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 0, next_cursor: null, candidates: [] })
      if (path.startsWith('reporting/workspaces/categorization/rule-suggestions')) {
        return Promise.resolve({
          suggestions: suggestionApplied ? [] : [{
            merchant_key: 'PIZZA HUT', category: 'Food & Dining / Dining Out', occurrences: 3,
            match: { merchant_canonical: 'PIZZA HUT' }, label: 'You categorized PIZZA HUT as Food & Dining / Dining Out 3 times. Make it a rule?',
          }],
        })
      }
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-pizza',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-pizza' }, payload: { category: 'Food & Dining / Dining Out' } }],
        impact: { category: { predicate_matches: 3, effective_assignments: 3, newly_categorized: 3, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } },
      })
      if (path === 'batch') { suggestionApplied = true; return Promise.resolve({ status: 'recorded' }) }
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    expect(await screen.findByText(/You categorized PIZZA HUT as Food & Dining \/ Dining Out 3 times\. Make it a rule\?/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Make it a rule' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      items: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-pizza' }, payload: { category: 'Food & Dining / Dining Out' } }],
    })))
    await waitFor(() => expect(screen.queryByText(/Make it a rule\?/)).not.toBeInTheDocument())
  })

  it('recovers from a stale-generation error by reloading the queue and reopening the same candidate, keeping the draft (Step 6f)', async () => {
    let staleThrown = false
    apiMock.mockImplementation((path: string) => {
      if (path === 'overview') return Promise.resolve({ generation: 'g2' })
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({
        candidate: { candidate_id: 'c1', label: 'CAFE SOCIAL' },
        records: [
          { anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL #1' },
          { anchor: 'a2', account: 'acct', currency: 'SGD', amount: '-5.00', date: '2026-01-05', direction: 'OUT', active: true, description: 'CAFE SOCIAL #2' },
        ],
      })
      if (path === 'categorization/derive-rule') {
        if (!staleThrown) { staleThrown = true; return Promise.reject(new Error('STALE_GENERATION: reload and preview again.')) }
        return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    // The stale derive-rule call triggered recovery; the same candidate reopened with the fresh generation.
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('overview', undefined))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('categorization/derive-rule', { generation: 'g2', anchors: ['a1', 'a2'] }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Review CAFE SOCIAL' })).toBeInTheDocument()
  })

  it('shows read-only refund context on an incoming-queue refund row (Step 6e)', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'GRAB', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { IN: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({
        candidate: { label: 'GRAB' },
        records: [{
          anchor: 'a1', account: 'acct', currency: 'SGD', amount: '8.00', date: '2026-01-01', direction: 'IN', active: true,
          description: 'GRAB REFUND', detail_bucket: 'refunds', applicable_fields: { category: false, cost_behavior: false, need: false, incoming_type: true },
          refund_context: 'Refund from Grab (usually Transport / Taxi)',
        }],
      })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /GRAB/ }))
    expect(await screen.findByText(/Refund from Grab \(usually Transport \/ Taxi\)/)).toBeInTheDocument()
  })

  it('reloads the queue with "Largest total first" when the sort control changes (Step 6g)', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 0, next_cursor: null, candidates: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    await screen.findByText(/0 groups need review/)
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort categorization groups' }), { target: { value: 'amount' } })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('sort=amount'), undefined))
  })

  it('refetches groups when the candidate kind filter changes, chosen from the Filters popover', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 0, next_cursor: null, candidates: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    await screen.findByText(/0 groups need review/)
    fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
    fireEvent.change(screen.getByLabelText('Filter categorization candidate kind'), { target: { value: 'missing_merchant' } })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('kind=missing_merchant'), undefined))
  })

  it('switches to an authoritative quick-discovery focus tab and reflects it in the panel title', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0, combined_completeness: { numerator: 3, denominator: 4 } })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 0, next_cursor: null, candidates: [], focus: new URLSearchParams(path.split('?')[1]).get('focus') })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    await screen.findByText(/0 groups need review/)
    expect(screen.getByText('75%')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Missing spending details/ }))
    await screen.findByText(/0 groups match missing spending details/)
  })

  it('does not inherit dashboard reporting dates for grouped or detail reads', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} accounts={['acct']} currencies={['SGD']} onSaved={() => {}} />)
    await screen.findByText(/1 group needs review/)
    fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
    fireEvent.change(screen.getByLabelText('Filter categorization account'), { target: { value: 'acct' } })
    fireEvent.change(screen.getByLabelText('Filter categorization currency'), { target: { value: 'SGD' } })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('account=acct'), undefined))
    fireEvent.click(screen.getByRole('button', { name: /CAFE SOCIAL/ }))
    await waitFor(() => expect(
      apiMock.mock.calls
        .filter(([path]) => String(path).startsWith('reporting/workspaces/categorization/candidates'))
        .every(([path]) => !String(path).match(/[?&](start|end)=/)),
    ).toBe(true))
  })

  it('auto-previews and saves one reusable rule for category, spending pattern/need and incoming type, with no manual preview click', async () => {
    const applicableRecords = twoRecordCandidate()
    applicableRecords.records.forEach(row => Object.assign(row, { applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } }))
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') {
        return Promise.resolve({
          rule_id: 'rule-abc',
          commands: [
            { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, category: 'Food & Dining / Groceries', reason: 'frequent' } },
            { kind: 'ROLE_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, cost_behavior: 'variable_routine', need: null, reason: 'frequent' } },
          ],
          impact: {
            category: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 },
            role: { matches: 2, will_change: 2, protected_overrides: 0 },
          },
        })
      }
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    expect(screen.getByRole('combobox', { name: 'Set spending pattern' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Set spending need' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Set incoming type' })).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByRole('combobox', { name: 'Set spending pattern' }), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'frequent' } })

    expect(await screen.findByText(/2 transactions will change/, {}, { timeout: 2000 })).toBeInTheDocument()
    expect(screen.getByText(/Spending pattern\/need: 2 matches; 2 will change/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      items: [
        { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, category: 'Food & Dining / Groceries', reason: 'frequent' } },
        { kind: 'ROLE_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, cost_behavior: 'variable_routine', need: null, reason: 'frequent' } },
      ],
    })))
  })

  it('classifies the unresolved inflows on screen alongside an incoming-type rule, since the rule only suggests', async () => {
    const payroll = {
      candidate: { label: 'ACME PAYROLL' },
      records: [
        { anchor: 'in1', account: 'acct', currency: 'SGD', amount: '4200.00', date: '2026-09-01', direction: 'IN', active: true, description: 'ACME PAYROLL', detail_bucket: 'pending_review', applicable_fields: { category: true, cost_behavior: false, need: false, incoming_type: true }, effective: { incoming_type: null } },
        { anchor: 'in0', account: 'acct', currency: 'SGD', amount: '5000.00', date: '2026-08-01', direction: 'IN', active: true, description: 'ACME PAYROLL', detail_bucket: 'income', applicable_fields: { category: true, cost_behavior: false, need: false, incoming_type: true }, effective: { incoming_type: 'income' } },
      ],
    }
    const previewBodies: Record<string, unknown>[] = []
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'description', label: 'ACME PAYROLL', transaction_count: 2, active_count: 2, date_range: { first: '2026-08-01', last: '2026-09-01' }, accounts: ['acct'], directions: { IN: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(payroll)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'Transactions whose description contains this text', match: { description_contains: 'ACME PAYROLL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-in', commands: [{ kind: 'INBOUND_RULE_SET', anchors: { rule_id: 'rule-in' }, payload: { classification: 'income' } }],
        impact: { incoming_type: { inbound_matches: 2, still_unresolved: 1 } },
      })
      if (path === 'reporting/workspaces/classification/preview') {
        previewBodies.push(body as Record<string, unknown>)
        return Promise.resolve({ generation: 'g1', changed_transactions: 1, command_count: 1, commands: [{ kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'in1' }, payload: { classification: 'income' } }], by_currency: [], fields: {}, rows: [] })
      }
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /ACME PAYROLL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.change(screen.getByRole('combobox', { name: 'Set incoming type' }), { target: { value: 'income' } })

    expect(await screen.findByText(/Also classifies the inflow shown here as Income now/, {}, { timeout: 2000 })).toBeInTheDocument()
    // Only the still-unresolved row is classified; one already confirmed is left alone.
    expect(previewBodies.at(-1)).toMatchObject({ anchors: ['in1'], incoming_type: { intent: 'set', value: 'income' }, policy: 'fill_missing' })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      items: [
        { kind: 'INBOUND_RULE_SET', anchors: { rule_id: 'rule-in' }, payload: { classification: 'income' } },
        { kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'in1' }, payload: { classification: 'income' } },
      ],
    })))
  })

  it('offers an undo toast after saving a rule, and Undo retires the same rule commands', async () => {
    const applicableRecords = twoRecordCandidate()
    const batchCalls: Record<string, unknown>[] = []
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Food & Dining / Groceries' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } },
      })
      if (path === 'batch') { batchCalls.push(body as Record<string, unknown>); return Promise.resolve({ status: 'recorded' }) }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    const saveButton = await screen.findByRole('button', { name: 'Save' }, { timeout: 2000 })
    fireEvent.click(saveButton)
    expect(await screen.findByText(/Saved rule for CAFE SOCIAL\. Undo/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(batchCalls).toHaveLength(2))
    expect(batchCalls[1].items).toEqual([
      { kind: 'CATEGORY_RULE_RETIRE', anchors: { rule_id: 'rule-abc' }, payload: { reason: expect.stringContaining('Undo:') } },
    ])
  })

  it('blocks Save on a new rule with a direction conflict until the user acknowledges it (Step 6d)', async () => {
    const applicableRecords = twoRecordCandidate()
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Income & Inflow / Earned Income' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 1, newly_categorized: 1, recategorized: 0, protected_by_override: 0, direction_conflict: 1 } },
      })
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Income & Inflow / Earned Income']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Income & Inflow / Earned Income' } })
    pickCategory('Income & Inflow / Earned Income')
    const saveButton = await screen.findByRole('button', { name: 'Save' }, { timeout: 2000 })
    expect(saveButton).toBeDisabled()
    const acknowledge = screen.getByRole('checkbox', { name: /leaves some transactions ambiguous/ })
    fireEvent.click(acknowledge)
    expect(saveButton).toBeEnabled()
    fireEvent.click(saveButton)
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
  })

  it('lets the Enter shortcut save a rule once the overlap warning is acknowledged', async () => {
    const applicableRecords = twoRecordCandidate()
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Income & Inflow / Earned Income' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 1, newly_categorized: 1, recategorized: 0, protected_by_override: 0, direction_conflict: 1 } },
      })
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Income & Inflow / Earned Income']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Income & Inflow / Earned Income' } })
    pickCategory('Income & Inflow / Earned Income')
    await screen.findByRole('button', { name: 'Save' }, { timeout: 2000 })
    fireEvent.click(screen.getByRole('checkbox', { name: /leaves some transactions ambiguous/ }))
    // The shortcut must agree with the Save button it mirrors: the handler is
    // registered once, so it has to read the acknowledgement from the current
    // render rather than the one it was first attached under.
    fireEvent.keyDown(document.body, { key: 'Enter' })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
  })

  it('does not offer an archived custom category for a new assignment', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(twoRecordCandidate())
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      if (path === 'categories') return Promise.resolve({ categories: [{ key: 'Legacy / Retired Thing', archived: true }] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current"
      categories={['Food & Dining / Groceries', 'Legacy / Retired Thing']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.focus(combobox)
    await screen.findByText('Food & Dining / Groceries')
    await waitFor(() => expect(screen.queryByRole('option', { name: /Legacy \/ Retired Thing/ })).not.toBeInTheDocument())
  })

  it('opens a "Same merchant as..." merge dialog from the review header, seeded with the group\'s own key', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', merchant_key: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({
        candidate: { kind: 'merchant', label: 'CAFE SOCIAL', merchant_key: 'CAFE SOCIAL' },
        records: [
          { anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL #1', merchant_normalized: 'CAFE SOCIAL SG013' },
          { anchor: 'a2', account: 'acct', currency: 'SGD', amount: '-5.00', date: '2026-01-05', direction: 'OUT', active: true, description: 'CAFE SOCIAL #2', merchant_normalized: 'CAFE SOCIAL SG014' },
        ],
      })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path.startsWith('merchants/similar')) return Promise.resolve({ generation: 'g1', merchant_key: 'CAFE SOCIAL', suggestions: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.click(screen.getByRole('button', { name: 'Same merchant as…' }))
    const mergeDialog = await screen.findByRole('dialog', { name: 'Merge merchants' })
    expect(within(mergeDialog).getByText('CAFE SOCIAL')).toBeInTheDocument()
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('merchants/similar'), undefined))
  })

  it('opens a rename control from the review header and saves it against the merchant key, never the display label', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', merchant_key: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { kind: 'merchant', label: 'CAFE SOCIAL', merchant_key: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={onSaved} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /This one/ })
    fireEvent.click(screen.getByRole('button', { name: 'Rename CAFE SOCIAL' }))
    fireEvent.change(screen.getByLabelText('New display name'), { target: { value: 'Cafe Social Corner' } })
    apiMock.mockResolvedValueOnce({
      generation: 'g1', merchant_key: 'CAFE SOCIAL', display_name: 'Cafe Social Corner',
      commands: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'CAFE SOCIAL' }, payload: { patterns: [{ exact: 'CAFE SOCIAL' }], replace_patterns: false, display_name: 'Cafe Social Corner', reason: 'Renamed merchant to Cafe Social Corner' } }],
      affected_transactions: 1,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview rename' }))
    expect(await screen.findByText(/for 1 transaction\./)).toBeInTheDocument()
    expect(apiMock).toHaveBeenCalledWith('categorization/merchant-rename/preview', {
      generation: 'g1', merchant_key: 'CAFE SOCIAL', display_name: 'Cafe Social Corner', reason: 'Renamed merchant to Cafe Social Corner',
    })
    apiMock.mockResolvedValueOnce({ status: 'applied' })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm rename' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('moves the active row with j/k and opens it with Enter', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 2, next_cursor: null, candidates: [
        { candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
        { candidate_id: 'c2', kind: 'merchant', label: 'ACME GROCER', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-02', last: '2026-01-02' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
      ] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c2')) return Promise.resolve({ candidate: { label: 'ACME GROCER' }, records: [] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    await screen.findByText(/2 groups need review/)
    const list = screen.getByText('Categorization inbox').closest('[data-slot=card]') as HTMLElement
    fireEvent.keyDown(list, { key: 'j' })
    fireEvent.keyDown(list, { key: 'Enter' })
    expect(await screen.findByRole('region', { name: 'Review ACME GROCER' })).toBeInTheDocument()
  })

  it('requires clicking exactly one transaction row before "This one" can be used, and names it', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(twoRecordCandidate())
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /This one/ }))
    expect(screen.getByText('Check exactly one transaction above to act on it.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('CAFE SOCIAL #2', { exact: false }).closest('button')!)
    expect(screen.getByRole('radio', { name: /This one — 5 Jan 2026/ })).toBeInTheDocument()
  })

  it('auto-previews then saves the selected-transactions scope through the shared classification draft, allowing the full trio', async () => {
    const preview = {
      generation: 'g1', changed_transactions: 2, command_count: 2,
      commands: [
        { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: '' } },
        { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a2' }, payload: { category: 'Food & Dining / Groceries', reason: '' } },
      ],
      by_currency: [{ currency: 'SGD', count: 2, changed: 2, absolute_amount: '9.00', category_changed: 2, behavior_changed: 0, need_changed: 0, incoming_changed: 0 }],
      fields: {
        category: { applicable: 2, not_applicable: 0, changed: 2, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        cost_behavior: { applicable: 0, not_applicable: 2, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        need: { applicable: 0, not_applicable: 2, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        incoming_type: { applicable: 0, not_applicable: 2, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
      },
      rows: [],
    }
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(twoRecordCandidate())
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'categories') return Promise.resolve({ categories: [] })
      if (path === 'reporting/workspaces/classification/preview') return Promise.resolve(preview)
      if (path === 'batch') return Promise.resolve({ status: 'applied' })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={onSaved} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /Selected/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.change(combobox, { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    await screen.findByText(/Ready to save 2 transactions/, {}, { timeout: 2000 })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
    expect(onSaved).toHaveBeenCalledWith('applied')
  })

  it('auto-previews and saves the grocery trio for a single transaction in the "This one" scope', async () => {
    const preview = {
      generation: 'g1', changed_transactions: 1, command_count: 2,
      commands: [
        { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: 'everyday groceries' } },
        { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: { cost_behavior: 'variable_routine', need: 'essential', reason: 'everyday groceries' } },
      ],
      by_currency: [{ currency: 'SGD', count: 1, changed: 1, absolute_amount: '4.00', category_changed: 1, behavior_changed: 1, need_changed: 1, incoming_changed: 0 }],
      fields: {
        category: { applicable: 1, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        cost_behavior: { applicable: 1, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        need: { applicable: 1, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        incoming_type: { applicable: 0, not_applicable: 1, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
      },
      rows: [],
    }
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-04', last: '2026-01-04' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      if (path === 'categories') return Promise.resolve({ categories: [] })
      if (path === 'reporting/workspaces/classification/preview') return Promise.resolve(preview)
      if (path === 'batch') return Promise.resolve({ status: 'applied' })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={onSaved} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /This one/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.change(combobox, { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText('Set spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Set spending need'), { target: { value: 'essential' } })
    fireEvent.change(screen.getByPlaceholderText('Why is this the right classification?'), { target: { value: 'everyday groceries' } })
    await screen.findByText(/Ready to save 1 transaction/, {}, { timeout: 2000 })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      reason: 'everyday groceries', items: preview.commands,
    })))
    expect(onSaved).toHaveBeenCalledWith('applied')
  })

  it('save and review next opens the correct next group from a freshly refreshed queue, not a stale pre-save index', async () => {
    let saved = false
    const candidatesBefore = [
      { candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
      { candidate_id: 'c2', kind: 'merchant', label: 'ACME GROCER', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-02', last: '2026-01-02' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
    ]
    // Saving CAFE SOCIAL resolves it, so a real refresh removes it from the
    // queue entirely rather than merely reordering it -- the case the old
    // stale-closure `queue[selectedIndex + 1]` logic got wrong.
    const candidatesAfter = [candidatesBefore[1]]
    const preview = {
      generation: 'g1', changed_transactions: 1, command_count: 1,
      commands: [{ kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: '' } }],
      by_currency: [], fields: {
        category: { applicable: 1, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        cost_behavior: { applicable: 0, not_applicable: 1, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        need: { applicable: 0, not_applicable: 1, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        incoming_type: { applicable: 0, not_applicable: 1, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
      },
      rows: [],
    }
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: saved ? 1 : 2, next_cursor: null, candidates: saved ? candidatesAfter : candidatesBefore })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c2')) return Promise.resolve({ candidate: { label: 'ACME GROCER' }, records: [{ anchor: 'b1', account: 'acct', currency: 'SGD', amount: '-6.00', date: '2026-01-02', direction: 'OUT', active: true, description: 'ACME GROCER' }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      if (path === 'categories') return Promise.resolve({ categories: [] })
      if (path === 'reporting/workspaces/classification/preview') return Promise.resolve(preview)
      if (path === 'batch') { saved = true; return Promise.resolve({ status: 'applied' }) }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.change(combobox, { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    await screen.findByText(/Ready to save/, {}, { timeout: 2000 })
    fireEvent.click(screen.getByRole('button', { name: 'Save & next' }))
    expect(await screen.findByRole('region', { name: 'Review ACME GROCER' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Review CAFE SOCIAL' })).not.toBeInTheDocument()
  })

  it('filters and creates categories from the combobox', async () => {
    apiMock.mockImplementation((path: string, body?: unknown) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-04', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      if (path === 'preview') return Promise.resolve({ valid: true })
      if (path === 'commands') return Promise.resolve({ status: 'applied' })
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.change(combobox, { target: { value: 'Groceries' } })
    expect(await screen.findByText('Food & Dining / Groceries')).toBeInTheDocument()
    fireEvent.change(combobox, { target: { value: 'Lifestyle / Hobbies' } })
    fireEvent.click(await screen.findByRole('button', { name: /Create "Lifestyle \/ Hobbies"/ }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('commands', expect.objectContaining({ kind: 'CATEGORY_CREATE', anchors: { category: 'Lifestyle / Hobbies' } })))
  })

  it('never shows "Incoming type" for a group of pure-spending rows', async () => {
    const records = [
      { anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'GRAB #1', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
      { anchor: 'a2', account: 'acct', currency: 'SGD', amount: '-5.00', date: '2026-01-02', direction: 'OUT', active: true, description: 'GRAB #2', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
      { anchor: 'a3', account: 'acct', currency: 'SGD', amount: '-6.00', date: '2026-01-03', direction: 'OUT', active: true, description: 'GRAB #3', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
    ]
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'GRAB', transaction_count: 3, active_count: 3, date_range: { first: '2026-01-01', last: '2026-01-03' }, accounts: ['acct'], directions: { OUT: 3 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'GRAB' }, records })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /GRAB/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /Selected/ }))
    expect(screen.queryByText('Incoming type')).not.toBeInTheDocument()
  })

  it('shows "Incoming type" scoped to the incoming rows only, and "Select incoming only" narrows the selection', async () => {
    const records = [
      { anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'GRAB #1', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
      { anchor: 'a2', account: 'acct', currency: 'SGD', amount: '-5.00', date: '2026-01-02', direction: 'OUT', active: true, description: 'GRAB #2', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
      { anchor: 'a3', account: 'acct', currency: 'SGD', amount: '-6.00', date: '2026-01-03', direction: 'OUT', active: true, description: 'GRAB #3', applicable_fields: { category: true, cost_behavior: true, need: true, incoming_type: false } },
      { anchor: 'a4', account: 'acct', currency: 'SGD', amount: '6.00', date: '2026-01-04', direction: 'IN', active: true, description: 'GRAB refund', applicable_fields: { category: true, cost_behavior: false, need: false, incoming_type: true } },
    ]
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'GRAB', transaction_count: 4, active_count: 4, date_range: { first: '2026-01-01', last: '2026-01-04' }, accounts: ['acct'], directions: { OUT: 3, IN: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'GRAB' }, records })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    expect(await screen.findByText('includes 1 incoming')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /GRAB/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /Selected/ }))
    expect(screen.getByText('Incoming type')).toBeInTheDocument()
    expect(screen.getByText(/incoming type applies to 1 of 4/)).toBeInTheDocument()
    expect(screen.getByText(/3 outgoing · 1 incoming/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Select incoming only' }))
    expect(screen.getByRole('radio', { name: /Selected \(1 of 4 loaded\)/ })).toBeInTheDocument()
  })

  it('hides every role/incoming field for a record missing applicable_fields entirely', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /This one/ })
    expect(screen.queryByLabelText('Set spending pattern')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Set spending need')).not.toBeInTheDocument()
    expect(screen.queryByText('Incoming type')).not.toBeInTheDocument()
  })

  it('ignores keyboard shortcuts while typing in a field, but applies a recent-category digit and "x" skip elsewhere', async () => {
    localStorage.setItem('fincl.classification.recentCategories', JSON.stringify(['Food & Dining / Groceries']))
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 2, next_cursor: null, candidates: [
        { candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-01', last: '2026-01-01' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
        { candidate_id: 'c2', kind: 'merchant', label: 'ACME GROCER', transaction_count: 1, active_count: 1, date_range: { first: '2026-01-02', last: '2026-01-02' }, accounts: ['acct'], directions: { OUT: 1 }, examples: [] },
      ] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve({ candidate: { label: 'CAFE SOCIAL' }, records: [{ anchor: 'a1', account: 'acct', currency: 'SGD', amount: '-4.00', date: '2026-01-01', direction: 'OUT', active: true, description: 'CAFE SOCIAL' }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c2')) return Promise.resolve({ candidate: { label: 'ACME GROCER' }, records: [] })
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    const combobox = await screen.findByRole('combobox', { name: 'Category' })
    fireEvent.keyDown(combobox, { key: '1' })
    expect((combobox as HTMLInputElement).value).toBe('')
    fireEvent.keyDown(document, { key: '1' })
    expect((combobox as HTMLInputElement).value).toBe('Food & Dining / Groceries')
    fireEvent.keyDown(document, { key: 'x' })
    expect(await screen.findByRole('region', { name: 'Review ACME GROCER' })).toBeInTheDocument()
  })

  it('"Enter" outside a field saves and advances once an auto-preview is ready', async () => {
    const applicableRecords = twoRecordCandidate()
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Food & Dining / Groceries' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } },
      })
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    await screen.findByRole('radio', { name: /All matching, now and later/ })
    fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled(), { timeout: 2000 })
    fireEvent.keyDown(document, { key: 'Enter' })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
  })

  it('still previews when the rule options arrive after the draft has settled', async () => {
    const applicableRecords = twoRecordCandidate()
    let releaseOptions: (value: unknown) => void = () => {}
    const lateOptions = new Promise(resolve => { releaseOptions = resolve })
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return lateOptions
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Food & Dining / Groceries' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } },
      })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /CAFE SOCIAL/ }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Category' }), { target: { value: 'Food & Dining / Groceries' } })
    pickCategory('Food & Dining / Groceries')
    // Let the 400ms auto-preview fire while derive-rule is still outstanding.
    await new Promise(resolve => setTimeout(resolve, 600))
    expect(apiMock).not.toHaveBeenCalledWith('reporting/workspaces/rules/compile-combined', expect.anything())
    releaseOptions({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled(), { timeout: 2000 })
  })

  it('the review panel readies its Save button 400ms after the draft settles, using fake timers', async () => {
    const applicableRecords = twoRecordCandidate()
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve({ counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 1, next_cursor: null, candidates: [{ candidate_id: 'c1', kind: 'merchant', label: 'CAFE SOCIAL', transaction_count: 2, active_count: 2, date_range: { first: '2026-01-01', last: '2026-01-05' }, accounts: ['acct'], directions: { OUT: 2 }, examples: [] }] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/c1')) return Promise.resolve(applicableRecords)
      if (path === 'categorization/derive-rule') return Promise.resolve({ options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'CAFE SOCIAL' } }] })
      if (path === 'reporting/workspaces/rules/compile-combined') return Promise.resolve({
        rule_id: 'rule-abc',
        commands: [{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { category: 'Food & Dining / Groceries' } }],
        impact: { category: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } },
      })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    vi.useFakeTimers()
    try {
      render(<CategorizationView generation="g1" freshness="current" categories={['Food & Dining / Groceries']} onSaved={() => {}} />)
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      fireEvent.click(screen.getByRole('button', { name: /CAFE SOCIAL/ }))
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByRole('radio', { name: /All matching, now and later/ })).toBeInTheDocument()
      fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'Food & Dining / Groceries' } })
      pickCategory('Food & Dining / Groceries')
      expect(screen.getByRole('button', { name: 'Waiting for preview…' })).toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(400) })
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    } finally { vi.useRealTimers() }
  })
})
