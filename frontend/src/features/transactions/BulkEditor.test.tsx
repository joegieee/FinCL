import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { Entry } from '@/lib/api'
import type { ClassificationPreview, FieldName, FieldOutcome } from '@/features/classification/api'
import type { TransactionRowOut } from './reportedApi'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { BulkEditor } = await import('./BulkEditor')

function entry(over: Partial<Entry> = {}): Entry {
  return {
    id: 'e1', anchor: 'a1', date: '2026-01-05', precision: 'DAY', amount: '-12.00', fee: '0',
    currency: 'SGD', account: 'Revolut', merchant: 'Acme Grocer', description: 'ACME GROCER OUTLET',
    category: 'Unclassified / Unclassified', basis: 'Not categorized', source_location: 'Lines 1–2',
    bucket: 'spending', eligible: true, review: true, source_family: 'CSV', lifecycle_state: 'COMPLETED',
    cost_behavior: 'unresolved', cost_behavior_basis: 'category default', need: 'unresolved', need_basis: 'category default',
    role_review_state: 'unresolved', suggested_classification: '', linked: false, classification: null,
    ...over,
  } as Entry
}

function row(over: Partial<Entry> = {}): TransactionRowOut {
  return {
    entry: entry(over),
    amount: { native: { value: '-12.00', currency: 'SGD' }, reported: null, purpose: 'FLOW', basis: 'identity', valuation_as_of: '2026-01-05', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: null },
    fee: null,
    statement_detail: {
      original: null, settled: null, explicit_fee: null, statement_cash_movement: null,
      printed_conversion: null, printed_charge_percent: null, effective_rate: null,
      validation: { status: 'not_checkable', reason: null },
      provenance: { document_record_id: null, source_location: null, detail_basis: 'unavailable', normalizer_version: 'v1' },
    },
  } as unknown as TransactionRowOut
}

function outcome(over: Partial<FieldOutcome> = {}): FieldOutcome {
  return { applicable: true, reason: null, before: null, after: null, changed: false, derived: false, manual_replacement: false, skipped_fill_missing: false, ...over }
}

function emptyFieldSummary() {
  const names: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
  return Object.fromEntries(names.map(n => [n, { applicable: 0, not_applicable: 0, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 }])) as ClassificationPreview['fields']
}

function preview(opts: {
  anchors: string[]
  fields?: Partial<Record<FieldName, FieldOutcome>>
  commands?: ClassificationPreview['commands']
  fieldSummary?: Partial<ClassificationPreview['fields']>
  byCurrency?: ClassificationPreview['by_currency']
}): ClassificationPreview {
  const names: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
  const fields = Object.fromEntries(names.map(n => [n, opts.fields?.[n] ?? outcome()])) as Record<FieldName, FieldOutcome>
  const commands = opts.commands ?? []
  return {
    generation: 'g1',
    changed_transactions: opts.anchors.length,
    command_count: commands.length,
    commands,
    by_currency: opts.byCurrency ?? [],
    fields: { ...emptyFieldSummary(), ...opts.fieldSummary },
    rows: opts.anchors.map(anchor => ({ anchor, currency: 'SGD', bucket: 'spending', fields })),
  }
}

function mockApi(opts: {
  onPreview?: (body: unknown) => ClassificationPreview
  onBatch?: (body: unknown) => { status: string }
  onAnchors?: (path: string) => { anchors: string[]; truncated: boolean }
  onDerive?: (body: unknown) => { generation: string; anchors: string[]; options: Array<{ label: string; match: Record<string, unknown> }>; unavailable_reason: string | null }
  onCategoryCompile?: (body: unknown) => unknown
  onRoleCompile?: (body: unknown) => unknown
}) {
  apiMock.mockImplementation((path: string, body?: unknown) => {
    if (path === 'categories') return Promise.resolve({ categories: [] })
    if (path === 'reporting/workspaces/classification/preview') return opts.onPreview ? Promise.resolve(opts.onPreview(body)) : Promise.reject(new Error('unexpected preview call'))
    if (path === 'batch') return opts.onBatch ? Promise.resolve(opts.onBatch(body)) : Promise.reject(new Error('unexpected batch call'))
    if (path.startsWith('reporting/transaction-anchors')) return opts.onAnchors ? Promise.resolve(opts.onAnchors(path)) : Promise.reject(new Error('unexpected anchors call'))
    if (path === 'categorization/derive-rule') return opts.onDerive ? Promise.resolve(opts.onDerive(body)) : Promise.reject(new Error('unexpected derive call'))
    if (path === 'reporting/workspaces/rules/compile') return opts.onCategoryCompile ? Promise.resolve(opts.onCategoryCompile(body)) : Promise.reject(new Error('unexpected category compile call'))
    if (path === 'financial-roles/rules/compile') return opts.onRoleCompile ? Promise.resolve(opts.onRoleCompile(body)) : Promise.reject(new Error('unexpected role compile call'))
    return Promise.reject(new Error(`unmocked path ${path}`))
  })
}

const filters = {
  start: '2026-01-01', end: '2026-01-31', pocket_currency: undefined, search: '', category: '',
  account: '', kind: '', direction: '', behavior: '', need: '', basis: '', unresolved_only: false, include_excluded: false,
}

function baseProps(over: Partial<Parameters<typeof BulkEditor>[0]> = {}): Parameters<typeof BulkEditor>[0] {
  return {
    generation: 'g1', freshness: 'current', categories: ['Food & Dining / Groceries', 'Lifestyle / Books'],
    rows: [row({ anchor: 'a1' }), row({ anchor: 'a2' }), row({ anchor: 'a3' })],
    picked: new Set(['a1', 'a2', 'a3']), matchAll: null, total: 10, filters,
    onSelectPage: vi.fn(), onSelectMatch: vi.fn(), onClear: vi.fn(), onSaved: vi.fn(),
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('BulkEditor — grocery trio across a multi-field batch', () => {
  it('applies category, pattern and need together to three selected expenses in one atomic batch', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { anchors: string[]; category: { intent: string; value: string }; cost_behavior: { intent: string; value: string }; need: { intent: string; value: string }; policy: string; reason: string }
        expect(draft.anchors).toEqual(['a1', 'a2', 'a3'])
        expect(draft.category).toEqual({ intent: 'set', value: 'Food & Dining / Groceries' })
        expect(draft.cost_behavior).toEqual({ intent: 'set', value: 'variable_routine' })
        expect(draft.need).toEqual({ intent: 'set', value: 'essential' })
        expect(draft.policy).toBe('fill_missing')
        expect(draft.reason).toBe('Weekly shop')
        return preview({
          anchors: ['a1', 'a2', 'a3'],
          commands: [
            { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: {} },
            { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} },
            { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a2' }, payload: {} },
            { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a2' }, payload: {} },
            { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a3' }, payload: {} },
            { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a3' }, payload: {} },
          ],
          fieldSummary: {
            category: { applicable: 3, not_applicable: 0, changed: 3, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
            cost_behavior: { applicable: 3, not_applicable: 0, changed: 3, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
            need: { applicable: 3, not_applicable: 0, changed: 3, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
            incoming_type: { applicable: 0, not_applicable: 3, changed: 0, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
          },
          byCurrency: [{ currency: 'SGD', count: 3, changed: 3, absolute_amount: '36.00', category_changed: 3, behavior_changed: 3, need_changed: 3, incoming_changed: 0 }],
        })
      },
      onBatch: body => {
        const batch = body as { items: unknown[]; reason: string }
        expect(batch.items).toHaveLength(6)
        return { status: 'applied' }
      },
    })
    const onSaved = vi.fn()
    const onClear = vi.fn()
    render(<BulkEditor {...baseProps({ onSaved, onClear })} />)

    fireEvent.change(screen.getByLabelText('Category'), { target: { value: 'Food & Dining / Groceries' } })
    fireEvent.click(await screen.findByText('Food & Dining / Groceries'))
    fireEvent.change(screen.getByLabelText('Set spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Set spending need'), { target: { value: 'essential' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Weekly shop' } })

    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save 3 transactions/)
    expect(screen.getByText(/Category applies to 3 of 3 selected, 3 changing/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(onClear).toHaveBeenCalled()
  })
})

describe('BulkEditor — cross-page selection', () => {
  it('sends every picked anchor exactly once, including anchors not on the currently loaded page', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { anchors: string[] }
        expect(draft.anchors).toEqual(['a1', 'a9'])
        return preview({ anchors: ['a1', 'a9'], commands: [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} }, { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a9' }, payload: {} }] })
      },
      onBatch: () => ({ status: 'applied' }),
    })
    render(<BulkEditor {...baseProps({
      rows: [row({ anchor: 'a1' })], // a9 came from an earlier page and is no longer loaded
      picked: new Set(['a1', 'a9']),
    })} />)

    expect(screen.getByText(/1 of 2 selected transactions are not on a currently loaded page/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Set spending pattern'), { target: { value: 'irregular' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'cross-page edit' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save 2 transactions/)
  })
})

describe('BulkEditor — all-matching scope', () => {
  it('freezes all matching anchors against the reported anchors endpoint using every active filter, and discloses a cap', async () => {
    mockApi({
      onAnchors: path => {
        const q = new URLSearchParams(path.split('?')[1])
        expect(q.get('history_start')).toBe('2026-01-01')
        expect(q.get('history_end')).toBe('2026-01-31')
        expect(q.get('generation')).toBe('g1')
        return { anchors: ['a1', 'a2'], truncated: true }
      },
    })
    const onSelectMatch = vi.fn()
    render(<BulkEditor {...baseProps({ picked: new Set(['a1', 'a2']), total: 250, onSelectMatch })} />)
    fireEvent.click(screen.getByRole('button', { name: /Select all 250 matching/ }))
    await waitFor(() => expect(onSelectMatch).toHaveBeenCalledWith({ anchors: ['a1', 'a2'], truncated: true }))
  })

  it('shows the capped-selection notice once all-matching is frozen and truncated', () => {
    mockApi({})
    render(<BulkEditor {...baseProps({ matchAll: { anchors: ['a1', 'a2'], truncated: true } })} />)
    const region = screen.getByRole('region', { name: 'Bulk assignment' })
    expect(region.textContent).toContain('2')
    expect(region.textContent).toContain('capped: narrow the filter to select more')
  })
})

describe('BulkEditor — fill-missing versus replace', () => {
  it('defaults to fill-missing and switches the submitted policy to replace', async () => {
    let seenPolicy = ''
    mockApi({
      onPreview: body => {
        seenPolicy = (body as { policy: string }).policy
        return preview({ anchors: ['a1', 'a2', 'a3'], commands: [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} }] })
      },
    })
    render(<BulkEditor {...baseProps()} />)
    fireEvent.change(screen.getByLabelText('Set spending need'), { target: { value: 'discretionary' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'r' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
    expect(seenPolicy).toBe('fill_missing')

    fireEvent.click(screen.getByLabelText(/Replace selected fields/))
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await waitFor(() => expect(seenPolicy).toBe('replace'))
  })
})

describe('BulkEditor — mixed-selection applicability', () => {
  it('discloses that a field applies only to a subset of the selection', async () => {
    mockApi({
      onPreview: () => preview({
        anchors: ['a1', 'a2', 'a3'],
        commands: [{ kind: 'INBOUND_CLASSIFICATION', anchors: { entry: 'a2' }, payload: {} }],
        fieldSummary: {
          incoming_type: { applicable: 1, not_applicable: 2, changed: 1, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 },
        },
      }),
    })
    render(<BulkEditor {...baseProps()} />)
    fireEvent.change(screen.getByLabelText('Incoming type'), { target: { value: 'income' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'salary' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Incoming type applies to 1 of 3 selected, 1 changing/)
  })
})

describe('BulkEditor — limit disclosure', () => {
  it('surfaces the server command-limit error instead of silently truncating', async () => {
    mockApi({ onPreview: () => { throw new Error('This selection would generate 420 decisions; narrow the selection to fit the 400-command limit.') } })
    render(<BulkEditor {...baseProps()} />)
    fireEvent.change(screen.getByLabelText('Set spending need'), { target: { value: 'discretionary' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'r' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/narrow the selection to fit the 400-command limit/)
  })
})

describe('BulkEditor — reusable rule from the selection (Step 7)', () => {
  it('reintroduces the deferred bulk role-rule affordance as a separate action from the row-edit save, distinct from the shared editor fields', async () => {
    mockApi({
      onPreview: () => preview({
        anchors: ['a1', 'a2', 'a3'],
        commands: [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} }],
        fieldSummary: { cost_behavior: { applicable: 3, not_applicable: 0, changed: 3, skipped_fill_missing: 0, manual_replacements: 0, derived: 0 } },
      }),
      onBatch: body => {
        const batch = body as { items: unknown[] }
        // The row-edit save carries only the previewed row commands -- the
        // rule is a distinct action, never folded silently into this batch.
        expect(batch.items).toHaveLength(1)
        return { status: 'applied' }
      },
      onDerive: body => {
        expect(body).toEqual({ generation: 'g1', anchors: ['a1', 'a2', 'a3'] })
        return { generation: 'g1', anchors: ['a1', 'a2', 'a3'], options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'Acme Grocer' } }], unavailable_reason: null }
      },
      onRoleCompile: body => {
        expect(body).toEqual({ generation: 'g1', match: { merchant_canonical: 'Acme Grocer' }, cost_behavior: 'variable_routine', need: null, reason: 'Weekly shop' })
        return { command: { kind: 'ROLE_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: {} }, impact: { matches: 3, will_change: 2, protected_overrides: 1 } }
      },
    })
    const onSaved = vi.fn()
    const onClear = vi.fn()
    render(<BulkEditor {...baseProps({ onSaved, onClear })} />)

    fireEvent.change(screen.getByLabelText('Set spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Weekly shop' } })

    // Save the row edit first -- the rule section below is untouched by it.
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(onClear).toHaveBeenCalled()

    // The optional rule is its own, separately reviewed and saved action.
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable spending rule', { exact: false }))
    fireEvent.click(screen.getByRole('button', { name: /Preview rule/ }))
    await screen.findByText('Spending-role rule impact')
    expect(screen.getByText(/1 manual override stays protected/)).toBeInTheDocument()
  })

  it('offers no reusable rule until a field to generalize has been chosen', () => {
    mockApi({})
    render(<BulkEditor {...baseProps()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    expect(screen.getByText(/Choose a category, spending pattern, or spending need above/)).toBeInTheDocument()
  })
})
