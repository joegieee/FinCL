import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { RuleSection } = await import('./RuleSection')

function mockApi(opts: {
  onDerive?: (body: unknown) => { generation: string; anchors: string[]; options: Array<{ label: string; match: Record<string, unknown> }>; unavailable_reason: string | null }
  onCategoryCompile?: (body: unknown) => unknown
  onRoleCompile?: (body: unknown) => unknown
  onBatch?: (body: unknown) => { status: string }
}) {
  apiMock.mockImplementation((path: string, body?: unknown) => {
    if (path === 'categorization/derive-rule') return opts.onDerive ? Promise.resolve(opts.onDerive(body)) : Promise.reject(new Error('unexpected derive call'))
    if (path === 'reporting/workspaces/rules/compile') return opts.onCategoryCompile ? Promise.resolve(opts.onCategoryCompile(body)) : Promise.reject(new Error('unexpected category compile call'))
    if (path === 'financial-roles/rules/compile') return opts.onRoleCompile ? Promise.resolve(opts.onRoleCompile(body)) : Promise.reject(new Error('unexpected role compile call'))
    if (path === 'batch') return opts.onBatch ? Promise.resolve(opts.onBatch(body)) : Promise.reject(new Error('unexpected batch call'))
    return Promise.reject(new Error(`unmocked path ${path}`))
  })
}

const ONE_OPTION = { generation: 'g1', anchors: ['a1'], options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'Acme Grocer' } }], unavailable_reason: null }
const NO_OPTIONS = { generation: 'g1', anchors: ['a1'], options: [], unavailable_reason: 'The selected transactions do not share a safe reusable predicate' }
const TWO_OPTIONS = {
  generation: 'g1', anchors: ['a1'], unavailable_reason: null,
  options: [
    { label: 'All transactions from this merchant', match: { merchant_canonical: 'Acme Grocer' } },
    { label: 'All transactions to this PayNow recipient', match: { paynow_to: 'acme grocer' } },
  ],
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('RuleSection', () => {
  it('starts collapsed and explains itself as a distinct, optional action', () => {
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={vi.fn()} />)
    expect(screen.getByRole('button', { name: /Also save a reusable rule/ })).toBeInTheDocument()
    expect(screen.queryByText(/Optional reusable rule/)).not.toBeInTheDocument()
  })

  it('disables rule creation until a category, pattern, or need is chosen', () => {
    render(<RuleSection generation="g1" anchors={['a1']} category="" costBehavior="" need="" reason="" onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    expect(screen.getByText(/Choose a category, spending pattern, or spending need above/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Check for a reusable rule/ })).not.toBeInTheDocument()
  })

  it('never auto-selects a predicate when more than one is available, and explains an unavailable predicate', async () => {
    mockApi({ onDerive: () => NO_OPTIONS })
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText(/do not share a safe reusable predicate/)
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
  })

  it('requires an explicit predicate choice among multiple options before previewing is possible', async () => {
    mockApi({ onDerive: () => TWO_OPTIONS })
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    expect(screen.getAllByRole('radio').every(r => !(r as HTMLInputElement).checked)).toBe(true)
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    expect(screen.getByRole('button', { name: /Preview rule/ })).toBeDisabled()
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    expect(screen.getByRole('button', { name: /Preview rule/ })).not.toBeDisabled()
  })

  it('previews and saves a category rule alone, distinctly labeled and separate from any row edit', async () => {
    mockApi({
      onDerive: () => ONE_OPTION,
      onCategoryCompile: body => {
        expect(body).toEqual({ match: { merchant_canonical: 'Acme Grocer' }, category: 'Food & Dining / Groceries', reason: 'weekly shop' })
        return {
          command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: { match: { merchant_canonical: 'Acme Grocer' }, category: 'Food & Dining / Groceries', reason: 'weekly shop' } },
          impact: { predicate_matches: 5, effective_assignments: 5, newly_categorized: 3, recategorized: 0, protected_by_override: 1, direction_conflict: 0 },
        }
      },
      onBatch: body => {
        const batch = body as { items: Array<{ kind: string }> }
        expect(batch.items).toEqual([{ kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: { match: { merchant_canonical: 'Acme Grocer' }, category: 'Food & Dining / Groceries', reason: 'weekly shop' } }])
        return { status: 'applied' }
      },
    })
    const onSaved = vi.fn()
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={onSaved} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    fireEvent.click(screen.getByRole('button', { name: /Preview rule/ }))

    await screen.findByText('Category rule impact')
    expect(screen.queryByText('Spending-role rule impact')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save rule/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('previews a category rule and a spending-role rule together as two distinct impact blocks, saved as one atomic batch', async () => {
    mockApi({
      onDerive: () => ONE_OPTION,
      onCategoryCompile: () => ({
        command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: {} },
        impact: { predicate_matches: 5, effective_assignments: 5, newly_categorized: 3, recategorized: 0, protected_by_override: 0, direction_conflict: 0 },
      }),
      onRoleCompile: body => {
        expect(body).toEqual({ generation: 'g1', match: { merchant_canonical: 'Acme Grocer' }, cost_behavior: 'variable_routine', need: 'essential', reason: 'weekly shop' })
        return { command: { kind: 'ROLE_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: {} }, impact: { matches: 5, will_change: 4, protected_overrides: 1 } }
      },
      onBatch: body => {
        const batch = body as { items: Array<{ kind: string }> }
        expect(batch.items.map(i => i.kind)).toEqual(['CATEGORY_RULE_SET', 'ROLE_RULE_SET'])
        return { status: 'applied' }
      },
    })
    const onSaved = vi.fn()
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="variable_routine" need="essential" reason="weekly shop" onSaved={onSaved} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    fireEvent.click(screen.getByLabelText('Reusable spending rule', { exact: false }))
    fireEvent.click(screen.getByRole('button', { name: /Preview rule/ }))

    await screen.findByText('Category rule impact')
    await screen.findByText('Spending-role rule impact')
    expect(screen.getByText(/1 manual override stays protected/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save rule/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('blocks preview until a reason is present, reusing the caller-provided reason rather than inventing its own', async () => {
    mockApi({ onDerive: () => ONE_OPTION })
    render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="" onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    expect(screen.getByText(/A reason is required above/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Preview rule/ })).toBeDisabled()
  })

  it('invalidates a compiled preview when the selection changes', async () => {
    mockApi({
      onDerive: () => ONE_OPTION,
      onCategoryCompile: () => ({ command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: {} }, impact: { predicate_matches: 1, effective_assignments: 1, newly_categorized: 1, recategorized: 0, protected_by_override: 0, direction_conflict: 0 } }),
    })
    const { rerender } = render(<RuleSection generation="g1" anchors={['a1']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    fireEvent.click(screen.getByRole('button', { name: /Preview rule/ }))
    await screen.findByText('Category rule impact')

    rerender(<RuleSection generation="g1" anchors={['a1', 'a2']} category="Food & Dining / Groceries" costBehavior="" need="" reason="weekly shop" onSaved={vi.fn()} />)
    expect(screen.queryByText('Category rule impact')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Check for a reusable rule/ })).toBeInTheDocument()
  })
})
