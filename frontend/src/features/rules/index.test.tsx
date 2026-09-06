import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status: number; constructor(m: string, s: number) { super(m); this.status = s } },
}))

const loadRules = vi.fn()
const loadGroupedRules = vi.fn()
const compileRule = vi.fn()
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, loadRules: () => loadRules(), loadGroupedRules: () => loadGroupedRules(), compileRule: (m: unknown, c: string, r: string) => compileRule(m, c, r) }
})

const { RulesView } = await import('./index')
const CATEGORIES = ['Food & Dining / Groceries', 'Lifestyle / Books', 'Internal / Transfer']

afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => { loadGroupedRules.mockResolvedValue({ generation: 'g1', grouped: [] }) })

describe('RulesView', () => {
  it('checks a rule for its match count, then previews and commits it', async () => {
    loadRules.mockResolvedValue({ generation: 'g1', aliases: [], rules: [] })
    const onSaved = vi.fn()
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText(/0 rules/)

    const builder = within(document.querySelector('.rule-builder') as HTMLElement)
    fireEvent.change(builder.getByLabelText('Value'), { target: { value: 'CAFE SOCIAL' } })
    fireEvent.change(builder.getByLabelText('Category'), { target: { value: CATEGORIES[0] } })
    fireEvent.change(builder.getByLabelText('Reason'), { target: { value: 'frequent' } })

    compileRule.mockResolvedValueOnce({
      command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-abc' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, category: CATEGORIES[0], reason: 'frequent' } },
      match_count: 4,
      impact: { predicate_matches: 4, effective_assignments: 4, newly_categorized: 4, recategorized: 0, protected_by_override: 0, direction_conflict: 0 },
    })
    fireEvent.click(builder.getByRole('button', { name: 'Check match' }))
    expect(await screen.findByText(/4 transactions will change/)).toBeInTheDocument()

    apiMock.mockResolvedValueOnce({}) // preview
    fireEvent.click(builder.getByRole('button', { name: 'Preview' }))
    await screen.findByText('Ready to save this rule.')

    apiMock.mockResolvedValueOnce({ status: 'recorded' }) // commit
    fireEvent.click(builder.getByRole('button', { name: 'Save rule' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(apiMock.mock.calls[0][0]).toBe('preview')
    expect((apiMock.mock.calls[0][1] as { anchors: { rule_id: string } }).anchors.rule_id).toBe('rule-abc')
  })

  it('disables rule and alias writes when the ledger is stale', async () => {
    loadRules.mockResolvedValue({ generation: 'g1', aliases: [{ canonical: 'KFC', patterns: [{ exact: 'KFC' }], display_name: null, reason: 'x', journal_seq: 2, metrics: { observed_matches: 0, effective_assignments: 0, first_match: null, last_match: null } }], rules: [] })
    render(<RulesView generation="g1" freshness="stale" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText(/Refresh before editing rules/)
    for (const button of screen.getAllByRole('button', { name: /Retire|Split/ })) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    }
  })

  it('checks, previews, and restores a retired rule with a new reason', async () => {
    loadRules.mockResolvedValue({
      generation: 'g1', aliases: [], rules: [],
      inactive_rules: [{
        rule_id: 'rule-old', match: { merchant_canonical: 'CAFE SOCIAL' }, group_name: 'Food & Dining', category: 'Groceries', reason: 'duplicate', journal_seq: 4,
        metrics: { predicate_matches: 0, effective_assignments: 0, overridden: 0, direction_conflicts: 0, first_match: null, last_match: null },
      }],
    })
    const onSaved = vi.fn()
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText(/1 retired rule/)
    const retiredRow = document.querySelector('.inactive-rule-row') as HTMLElement
    fireEvent.change(within(retiredRow).getByLabelText('Reason'), { target: { value: 'Merchant is active again' } })
    compileRule.mockResolvedValueOnce({
      command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-old' }, payload: { match: { merchant_canonical: 'CAFE SOCIAL' }, category: CATEGORIES[0], reason: 'Merchant is active again' } },
      match_count: 2,
      impact: { predicate_matches: 2, effective_assignments: 2, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 },
    })
    fireEvent.click(within(retiredRow).getByRole('button', { name: 'Check reactivation impact' }))
    expect(await within(retiredRow).findByText(/Reactivation will assign 2 matching transactions/)).toBeInTheDocument()

    apiMock.mockResolvedValueOnce({})
    fireEvent.click(within(retiredRow).getByRole('button', { name: 'Preview restore' }))
    expect(await within(retiredRow).findByText('Ready to restore this rule.')).toBeInTheDocument()

    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(within(retiredRow).getByRole('button', { name: 'Restore rule' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(compileRule).toHaveBeenCalledWith({ merchant_canonical: 'CAFE SOCIAL' }, CATEGORIES[0], 'Merchant is active again')
  })

  it('renames a merchant alias in place, keeping the same canonical it matches against', async () => {
    loadRules.mockResolvedValue({
      generation: 'g1', rules: [],
      aliases: [{ canonical: 'KFC', patterns: [{ exact: 'KFC' }], display_name: null, reason: 'x', journal_seq: 2, metrics: { observed_matches: 3, effective_assignments: 3, first_match: null, last_match: null } }],
    })
    const onSaved = vi.fn()
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText('KFC')
    const aliases = within(document.querySelector('.alias-section') as HTMLElement)
    fireEvent.click(aliases.getByRole('button', { name: 'Rename KFC' }))
    fireEvent.change(aliases.getByLabelText('New display name'), { target: { value: 'KFC Singapore' } })
    apiMock.mockResolvedValueOnce({
      generation: 'g1', merchant_key: 'KFC', display_name: 'KFC Singapore',
      commands: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'KFC' }, payload: { patterns: [{ exact: 'KFC' }], replace_patterns: false, display_name: 'KFC Singapore', reason: 'Renamed merchant to KFC Singapore' } }],
      affected_transactions: 3,
    })
    fireEvent.click(aliases.getByRole('button', { name: 'Preview rename' }))
    expect(await aliases.findByText(/for 3 transactions/)).toBeInTheDocument()
    expect(apiMock).toHaveBeenCalledWith('categorization/merchant-rename/preview', {
      generation: 'g1', merchant_key: 'KFC', display_name: 'KFC Singapore', reason: 'Renamed merchant to KFC Singapore',
    })
    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(aliases.getByRole('button', { name: 'Confirm rename' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      reason: 'Renamed merchant to KFC Singapore',
      items: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'KFC' }, payload: { patterns: [{ exact: 'KFC' }], replace_patterns: false, display_name: 'KFC Singapore', reason: 'Renamed merchant to KFC Singapore' } }],
    }))
  })

  it('shows one grouped row per rule_id and edits or retires every kind it carries', async () => {
    loadRules.mockResolvedValue({ generation: 'g1', aliases: [], rules: [] })
    loadGroupedRules.mockResolvedValue({
      generation: 'g1',
      grouped: [{
        rule_id: 'rule-abc', match: { merchant_canonical: 'GRAB' }, match_label: 'Merchant: GRAB',
        category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'discretionary', incoming_type: null,
        kinds: ['category', 'role'], matches: 214, last_changed: '2026-09-14T00:00:00Z', reason: 'frequent',
      }],
    })
    apiMock.mockImplementation((path: string) => {
      if (path === 'reporting/workspaces/rules/compile-combined') {
        return Promise.resolve({
          rule_id: 'rule-abc',
          commands: [{ kind: 'ROLE_RULE_RETIRE', anchors: { rule_id: 'rule-abc' }, payload: { reason: 'drop role' } }],
          impact: {},
        })
      }
      if (path === 'batch') return Promise.resolve({ status: 'recorded' })
      if (path === "reporting/workspaces/rules/rule-abc/retire-combined/preview") {
        return Promise.resolve({
          rule_id: 'rule-abc',
          commands: [
            { kind: 'CATEGORY_RULE_RETIRE', anchors: { rule_id: 'rule-abc' }, payload: { reason: 'Retire rule rule-abc' } },
            { kind: 'ROLE_RULE_RETIRE', anchors: { rule_id: 'rule-abc' }, payload: { reason: 'Retire rule rule-abc' } },
          ],
          impact: {},
        })
      }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText(/Merchant: GRAB/)
    expect(screen.getByText(/214 matches/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByRole('combobox', { name: 'Spending pattern' }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check impact' }))
    await screen.findByRole('button', { name: 'Save changes' })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      items: [{ kind: 'ROLE_RULE_RETIRE', anchors: { rule_id: 'rule-abc' }, payload: { reason: 'drop role' } }],
    }))

    fireEvent.click(screen.getByRole('button', { name: 'Retire' }))
    await screen.findByText(/Retiring removes 2 rule kinds/)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm retire' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
  })

  it('splits a merchant alias after previewing the rules that still point at it', async () => {
    loadRules.mockResolvedValue({
      generation: 'g1', rules: [], inactive_rules: [],
      aliases: [{ canonical: 'KFC', patterns: [{ exact: 'KFC PTE LTD' }], display_name: null, reason: 'normalize', journal_seq: 2, metrics: { observed_matches: 1, effective_assignments: 1, first_match: '2026-01-01', last_match: '2026-01-01' } }],
    })
    loadGroupedRules.mockResolvedValue({
      generation: 'g1',
      grouped: [{
        rule_id: 'rule-1', match: { merchant_canonical: 'KFC' }, match_label: 'Merchant: KFC',
        category: 'Food & Dining / Groceries', cost_behavior: null, need: null, incoming_type: null,
        kinds: ['category'], matches: 3, last_changed: null, reason: 'r',
      }],
    })
    apiMock.mockImplementation((path: string) => {
      if (path === 'preview') return Promise.resolve({})
      if (path === 'commands') return Promise.resolve({ status: 'recorded' })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText('KFC')
    const aliases = within(document.querySelector('.alias-section') as HTMLElement)
    fireEvent.click(aliases.getByRole('button', { name: 'Split' }))
    expect(await aliases.findByText(/1 rule still points at "KFC"/)).toBeInTheDocument()
    fireEvent.click(aliases.getByRole('button', { name: 'Preview split' }))
    await waitFor(() => expect(aliases.getByRole('button', { name: 'Confirm split' })).toBeInTheDocument())
    fireEvent.click(aliases.getByRole('button', { name: 'Confirm split' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(apiMock).toHaveBeenCalledWith('commands', expect.objectContaining({ kind: 'MERCHANT_ALIAS_RETIRE', anchors: { canonical: 'KFC' } }))
  })

  it('opens the merge dialog from the multi-select "Merge selected" button', async () => {
    loadRules.mockResolvedValue({
      generation: 'g1', rules: [], inactive_rules: [],
      aliases: [
        { canonical: 'KFC', patterns: [{ exact: 'KFC' }], display_name: null, reason: 'x', journal_seq: 1, metrics: { observed_matches: 1, effective_assignments: 1, first_match: null, last_match: null } },
        { canonical: 'KFC EXPRESS', patterns: [{ exact: 'KFC EXPRESS' }], display_name: null, reason: 'x', journal_seq: 2, metrics: { observed_matches: 1, effective_assignments: 1, first_match: null, last_match: null } },
      ],
    })
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('merchants/similar')) return Promise.resolve({ generation: 'g1', merchant_key: 'KFC', suggestions: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText('KFC')
    const aliases = within(document.querySelector('.alias-section') as HTMLElement)
    fireEvent.click(aliases.getByRole('checkbox', { name: 'Select KFC to merge' }))
    fireEvent.click(aliases.getByRole('checkbox', { name: 'Select KFC EXPRESS to merge' }))
    fireEvent.click(aliases.getByRole('button', { name: 'Merge selected (2)' }))
    expect(await screen.findByRole('dialog', { name: 'Merge merchants' })).toBeInTheDocument()
  })

  it('shows merchant-alias journal history', async () => {
    loadRules.mockResolvedValue({
      generation: 'g1', rules: [], inactive_rules: [],
      aliases: [{ canonical: 'KFC', patterns: [{ exact: 'KFC PTE LTD' }], display_name: null, reason: 'normalize', journal_seq: 2, metrics: { observed_matches: 1, effective_assignments: 1, first_match: '2026-01-01', last_match: '2026-01-01' } }],
    })
    apiMock.mockResolvedValueOnce({ history: [{ journal_seq: 2, kind: 'MERCHANT_ALIAS_SET', recorded_at: '2026-01-01T00:00:00Z', reason: 'normalize' }] })
    render(<RulesView generation="g1" freshness="current" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText('KFC')
    const aliases = document.querySelector('.alias-section') as HTMLElement
    fireEvent.click(within(aliases).getByRole('button', { name: 'History' }))
    expect(await within(aliases).findByText(/merchant alias set/)).toBeInTheDocument()
    expect(apiMock).toHaveBeenCalledWith('aliases/KFC/history', undefined)
  })
})
