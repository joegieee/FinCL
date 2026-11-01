import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ClassificationPreview, FieldName, FieldOutcome } from '@/features/classification/api'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const loadInboundRules = vi.fn()
const compileInboundRule = vi.fn()
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    loadInboundRules: () => loadInboundRules(),
    compileInboundRule: (g: string, m: unknown, c: string, r: string) => compileInboundRule(g, m, c, r),
  }
})

const loadReportedCandidates = vi.fn()
const loadReportingPolicy = vi.fn()
vi.mock('./reportedApi', () => ({
  loadReportedCandidates: () => loadReportedCandidates(),
  loadReportingPolicy: (...a: unknown[]) => loadReportingPolicy(...a),
  nativeCandidates: (p: { native: unknown }) => p.native,
}))

const { InflowsView } = await import('./index')

const NATIVE_CANDIDATE = {
  candidate: 'detv1:acme',
  merchant_key: 'Acme Payroll',
  currency: 'USD',
  account_ids: ['acc-1'],
  cadence: 'monthly',
  source_count: 4,
  first_observed_on: '2026-03-25',
  last_receipt_on: '2026-06-25',
  confidence: 'likely',
  confidence_reason: 'grouped from recorded receipts only',
  review_question: 'Is Acme Payroll a recurring income stream?',
  amount_variation: { basis: 'stable', min_amount: '5000.00', max_amount: '5000.00', mean_amount: '5000.00', latest_amount: '5000.00' },
  timing_window: { cadence: 'monthly', first_observed_on: '2026-03-25', last_observed_on: '2026-06-25', next_expected_between: ['2026-07-23', '2026-07-27'], day_of_month_min: 25, day_of_month_max: 25 },
  occurrences: [
    { anchor: 'a1', date: '2026-03-25', amount: '5000.00', description: 'ACME PAYROLL MAR', account: 'DBS Checking' },
    { anchor: 'a2', date: '2026-04-25', amount: '5000.00', description: 'ACME PAYROLL APR', account: 'DBS Checking' },
    { anchor: 'a3', date: '2026-05-25', amount: '5000.00', description: 'ACME PAYROLL MAY', account: 'DBS Checking' },
    { anchor: 'a4', date: '2026-06-25', amount: '5000.00', description: 'ACME PAYROLL JUN', account: 'DBS Checking' },
  ],
}

function valuedMoney(nativeValue: string, reportedValue: string | null, currency = 'SGD') {
  return {
    native: { value: nativeValue, currency: 'USD' },
    reported: reportedValue === null ? null : { value: reportedValue, currency },
    purpose: 'flow', basis: reportedValue === null ? null : 'monthly_proxy', valuation_as_of: '2026-06-25',
    rate_as_of: null, rate_period: '2026-06', rate_path: [], forward_policy: null,
    unavailable: reportedValue === null ? { reason: 'RATE_NOT_IMPORTED', message: 'no rate', remedy: 'import a rate' } : null,
  }
}

function reportedCandidate(over: Record<string, unknown> = {}) {
  return {
    candidate: 'detv1:acme',
    occurrences: [
      { anchor: 'a1', reported: valuedMoney('5000.00', '6250.00') },
      { anchor: 'a2', reported: valuedMoney('5000.00', '6250.00') },
      { anchor: 'a3', reported: valuedMoney('5000.00', '6250.00') },
      { anchor: 'a4', reported: valuedMoney('5000.00', '6250.00') },
    ],
    variation: {
      latest: valuedMoney('5000.00', '6250.00'), minimum: valuedMoney('5000.00', '6250.00'), maximum: valuedMoney('5000.00', '6250.00'),
      mean: { value: '6250.00', currency: 'SGD' }, priced_count: 4, total_count: 4,
    },
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1', reporting_currency: 'SGD', policy_version: 'v1', valuation_as_of: '2026-06-26',
    candidates: [reportedCandidate()],
    native: { generation: 'g1', candidates: [NATIVE_CANDIDATE], exclusions: { excluded_source_count: 2, rule: 'loan movements are removed' }, policy: 'Likely only' },
    ...over,
  }
}

function outcome(over: Partial<FieldOutcome> = {}): FieldOutcome {
  return { applicable: true, reason: null, before: null, after: null, changed: false, derived: false, manual_replacement: false, skipped_fill_missing: false, ...over }
}

function emptyFieldSummary() {
  const names: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
  return Object.fromEntries(names.map(n => [n, { applicable: 0, not_applicable: 0, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 }])) as ClassificationPreview['fields']
}

function classificationPreview(opts: {
  anchors: string[]
  fields?: Partial<Record<FieldName, FieldOutcome>>
  commands?: ClassificationPreview['commands']
  fieldSummary?: Partial<ClassificationPreview['fields']>
}): ClassificationPreview {
  const names: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
  const fields = Object.fromEntries(names.map(n => [n, opts.fields?.[n] ?? outcome()])) as Record<FieldName, FieldOutcome>
  const commands = opts.commands ?? []
  return {
    generation: 'g1',
    changed_transactions: opts.anchors.length,
    command_count: commands.length,
    commands,
    by_currency: [{ currency: 'USD', count: opts.anchors.length, changed: commands.length ? opts.anchors.length : 0, absolute_amount: '0', category_changed: 0, behavior_changed: 0, need_changed: 0, incoming_changed: commands.length }],
    fields: { ...emptyFieldSummary(), ...opts.fieldSummary },
    rows: opts.anchors.map(anchor => ({ anchor, currency: 'USD', bucket: 'pending_review', fields })),
  }
}

function mockApi(opts: {
  onPreview?: (body: unknown) => ClassificationPreview
  onBatch?: (body: unknown) => { status: string }
}) {
  apiMock.mockImplementation((path: string, body?: unknown) => {
    if (path === 'categories') return Promise.resolve({ categories: [] })
    if (path === 'reporting/workspaces/classification/preview') return opts.onPreview ? Promise.resolve(opts.onPreview(body)) : Promise.reject(new Error('unexpected preview call'))
    if (path === 'batch') return opts.onBatch ? Promise.resolve(opts.onBatch(body)) : Promise.reject(new Error('unexpected batch call'))
    return Promise.reject(new Error(`unmocked path ${path}`))
  })
}

const CATEGORIES = ['Food & Dining / Groceries', 'Income / Salary']

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('InflowsView', () => {
  it('confirms the selected receipts and an optional rule as one reviewed, atomic batch', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    mockApi({
      onPreview: body => {
        const draft = body as { anchors: string[]; category?: unknown; incoming_type: { intent: string; value: string }; policy: string; reason: string }
        expect(draft.anchors).toEqual(['a2', 'a3', 'a4'])
        expect(draft.category).toBeUndefined()
        expect(draft.incoming_type).toEqual({ intent: 'set', value: 'income' })
        expect(draft.policy).toBe('replace')
        expect(draft.reason).toBe('employer payslip')
        return classificationPreview({
          anchors: ['a2', 'a3', 'a4'],
          commands: [
            { kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a2' }, payload: { classification: 'income', reason: 'employer payslip' } },
            { kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a3' }, payload: { classification: 'income', reason: 'employer payslip' } },
            { kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a4' }, payload: { classification: 'income', reason: 'employer payslip' } },
          ],
          fieldSummary: { incoming_type: { applicable: 3, not_applicable: 0, changed: 3, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 } },
        })
      },
      onBatch: body => {
        const batch = body as { items: Array<{ kind: string; anchors: Record<string, string> }> }
        expect(batch.items.filter(i => i.kind === 'INBOUND_CLASSIFICATION').map(i => i.anchors.entry)).toEqual(['a2', 'a3', 'a4'])
        expect(batch.items.at(-1)?.kind).toBe('INBOUND_RULE_SET')
        return { status: 'recorded_refresh_pending' }
      },
    })
    const onSaved = vi.fn()
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={onSaved} />)

    await screen.findByText('Acme Payroll')
    const card = within(document.querySelector('.income-candidate') as HTMLElement)
    expect(card.getByText('Likely')).toBeInTheDocument()
    expect(screen.getByText('Reporting in SGD', { exact: false })).toBeInTheDocument()
    expect(card.getAllByText(/6,250\.00 SGD/).length).toBeGreaterThan(0)
    expect(card.getByText('ACME PAYROLL MAR')).toBeInTheDocument()
    expect(card.getAllByText('DBS Checking').length).toBe(4)

    // Drop one receipt from the selection; only the remaining anchors preview/save.
    fireEvent.click(card.getByLabelText('Include 25 Mar 2026'))
    fireEvent.change(card.getByLabelText('Reason'), { target: { value: 'employer payslip' } })
    fireEvent.click(card.getByLabelText(/save a reusable rule/))

    compileInboundRule.mockResolvedValueOnce({
      command: { kind: 'INBOUND_RULE_SET', anchors: { rule_id: 'rule-x' }, payload: { match: { description_contains: 'Acme Payroll' }, classification: 'income', reason: 'employer payslip' } },
      impact: { inbound_matches: 4, still_unresolved: 0 },
    })

    fireEvent.click(card.getByRole('button', { name: /Review changes/ }))
    await card.findByText(/Ready to save 3 transactions/)

    await card.findByText(/Rule impact: suggests this classification for 4 matching credits in/)
    expect(card.getByTestId('rule-impact')).toHaveTextContent('0 of which are still unresolved today')
    expect(card.getByTestId('rule-impact')).toHaveTextContent('it only suggests, so none of them are classified by saving this rule')

    fireEvent.click(card.getByRole('button', { name: /Save 3 receipts \+ rule/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded_refresh_pending'))
    expect(compileInboundRule).toHaveBeenCalledWith('g1', { description_contains: 'Acme Payroll' }, 'income', 'employer payslip')
  })

  it('shows the rule impact singular for exactly one match, and keeps the classification preview when the rule check fails', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    mockApi({
      onPreview: () => classificationPreview({
        anchors: ['a1', 'a2', 'a3', 'a4'],
        commands: [{ kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a1' }, payload: {} }],
        fieldSummary: { incoming_type: { applicable: 4, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 } },
      }),
    })
    compileInboundRule.mockRejectedValueOnce(new Error('This selection does not share a safe reusable predicate'))
    const onSaved = vi.fn()
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={onSaved} />)

    await screen.findByText('Acme Payroll')
    const card = within(document.querySelector('.income-candidate') as HTMLElement)
    fireEvent.change(card.getByLabelText('Reason'), { target: { value: 'employer payslip' } })
    fireEvent.click(card.getByLabelText(/save a reusable rule/))
    fireEvent.click(card.getByRole('button', { name: /Review changes/ }))

    // The rule check failed, but the classification preview it was reviewed
    // alongside is not discarded -- and saving is held back rather than
    // silently dropping the rule the user asked for.
    await card.findByText(/This selection does not share a safe reusable predicate/)
    await card.findByText(/Ready to save 4 transactions/)
    expect((card.getByRole('button', { name: /Save/ }) as HTMLButtonElement).disabled).toBe(true)

    // Unchecking the rule (no longer wanting it) and reviewing again allows
    // saving the classification alone.
    fireEvent.click(card.getByLabelText(/save a reusable rule/))
    fireEvent.click(card.getByRole('button', { name: /Review changes/ }))
    await card.findByText(/Ready to save 4 transactions/)
    expect((card.getByRole('button', { name: /Save 4 receipts/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('surfaces a singular still-unresolved count correctly', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    mockApi({
      onPreview: () => classificationPreview({ anchors: ['a1', 'a2', 'a3', 'a4'] }),
    })
    compileInboundRule.mockResolvedValueOnce({
      command: { kind: 'INBOUND_RULE_SET', anchors: { rule_id: 'rule-x' }, payload: { match: { description_contains: 'Acme Payroll' }, classification: 'income', reason: 'x' } },
      impact: { inbound_matches: 1, still_unresolved: 1 },
    })
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={vi.fn()} />)

    await screen.findByText('Acme Payroll')
    const card = within(document.querySelector('.income-candidate') as HTMLElement)
    fireEvent.change(card.getByLabelText('Reason'), { target: { value: 'x' } })
    fireEvent.click(card.getByLabelText(/save a reusable rule/))
    fireEvent.click(card.getByRole('button', { name: /Review changes/ }))
    await card.findByText(/Rule impact: suggests this classification for 1 matching credit in/)
    expect(card.getByTestId('rule-impact')).toHaveTextContent('1 of which is still unresolved today')
  })

  it('previews and saves an explicit category plus incoming type together for one receipt', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    mockApi({
      onPreview: body => {
        const draft = body as { category: { intent: string; value: string }; incoming_type: { intent: string; value: string } }
        expect(draft.category).toEqual({ intent: 'set', value: 'Income / Salary' })
        expect(draft.incoming_type).toEqual({ intent: 'set', value: 'income' })
        return classificationPreview({
          anchors: ['a1', 'a2', 'a3', 'a4'],
          commands: [{ kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: {} }, { kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a1' }, payload: {} }],
          fieldSummary: {
            category: { applicable: 4, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
            incoming_type: { applicable: 4, not_applicable: 0, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
          },
        })
      },
      onBatch: () => ({ status: 'applied' }),
    })
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={vi.fn()} />)

    await screen.findByText('Acme Payroll')
    const card = within(document.querySelector('.income-candidate') as HTMLElement)
    fireEvent.change(card.getByLabelText('Category'), { target: { value: 'Income / Salary' } })
    fireEvent.click(await card.findByText('Income / Salary'))
    fireEvent.change(card.getByLabelText('Reason'), { target: { value: 'payslip' } })
    fireEvent.click(card.getByRole('button', { name: /Review changes/ }))

    await card.findByText(/Ready to save 4 transactions/)
    expect(card.getByText(/Category applies to 4 of 4 selected, 1 changing/)).toBeInTheDocument()
  })

  it('blocks confirmation while the ledger is stale', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [{ rule_id: 'r1', match: { description_contains: 'Acme' }, classification: 'income', reason: 'kept', journal_seq: 3 }] })
    render(<InflowsView generation="g1" freshness="stale" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={vi.fn()} />)

    await screen.findByText('Acme Payroll')
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'payslip' } })
    expect((screen.getByRole('button', { name: /Review changes/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Retire' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a candidate with an unpriced receipt without dropping its count', async () => {
    loadReportedCandidates.mockResolvedValue(page({
      candidates: [reportedCandidate({
        occurrences: [
          { anchor: 'a1', reported: valuedMoney('5000.00', null) },
          { anchor: 'a2', reported: valuedMoney('5000.00', '6250.00') },
          { anchor: 'a3', reported: valuedMoney('5000.00', '6250.00') },
          { anchor: 'a4', reported: valuedMoney('5000.00', '6250.00') },
        ],
        variation: {
          latest: valuedMoney('5000.00', '6250.00'), minimum: valuedMoney('5000.00', '6250.00'), maximum: valuedMoney('5000.00', '6250.00'),
          mean: { value: '6250.00', currency: 'SGD' }, priced_count: 3, total_count: 4,
        },
      })],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={vi.fn()} />)

    await screen.findByText('Acme Payroll')
    expect(screen.getByText(/1 of 4 receipts could not be priced/)).toBeInTheDocument()
    expect(screen.getByText('No exchange rate has been imported for this date.')).toBeInTheDocument()
    // The candidate and all four native receipts remain visible/selectable.
    expect(within(document.querySelector('.income-occurrences') as HTMLElement).getAllByRole('checkbox')).toHaveLength(4)
  })

  it('retains the last-good response on a same-scope 409 and clears on first-load stale', async () => {
    const { ApiError } = await import('@/lib/api')
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    loadReportedCandidates.mockRejectedValueOnce(new ApiError('stale', 409))
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={vi.fn()} onSaved={vi.fn()} />)

    expect(await screen.findByText('Refresh required.')).toBeInTheDocument()

    loadReportedCandidates.mockResolvedValueOnce(page())
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await screen.findByText('Acme Payroll')

    loadReportedCandidates.mockRejectedValueOnce(new ApiError('stale', 409))
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('Showing the last loaded values for this exact scope.')).toBeInTheDocument()
    expect(screen.getByText('Acme Payroll')).toBeInTheDocument()
  })

  it('offers a discoverable route to classify one-off receipts in Transactions', async () => {
    loadReportedCandidates.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadInboundRules.mockResolvedValue({ generation: 'g1', rules: [] })
    const onOpenTransactions = vi.fn()
    render(<InflowsView generation="g1" freshness="current" categories={CATEGORIES} onOpenTransactions={onOpenTransactions} onSaved={vi.fn()} />)

    await screen.findByText('Acme Payroll')
    fireEvent.click(screen.getByRole('button', { name: /Open in Transactions/ }))
    expect(onOpenTransactions).toHaveBeenCalled()
  })
})
