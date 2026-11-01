import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { Dashboard, Entry } from '@/lib/api'
import type { ClassificationPreview, FieldName, FieldOutcome } from './api'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { EntryDialog } = await import('./SingleTransactionEditor')

function entry(over: Partial<Entry> = {}): Entry {
  return {
    id: 'e1', anchor: 'a1', date: '2026-01-05', precision: 'DAY', amount: '-12.00', fee: '0',
    currency: 'SGD', account: 'Revolut', merchant: 'Acme Grocer', description: 'ACME GROCER OUTLET',
    category: 'Unclassified / Unclassified', basis: 'Not categorized', source_location: 'Lines 1–2',
    bucket: 'spending', eligible: true, review: true, source_family: 'CSV', lifecycle_state: 'COMPLETED',
    cost_behavior: 'unresolved', cost_behavior_basis: 'category default', need: 'unresolved', need_basis: 'category default',
    role_review_state: 'unresolved', suggested_classification: '', linked: false, classification: null, debt_allocation: null,
    ...over,
  } as Entry
}

function dashboard(over: Partial<Dashboard> = {}): Dashboard {
  return {
    generation: 'g1', freshness: 'current',
    categories: ['Food & Dining / Groceries', 'Lifestyle / Books', 'Internal / Transfer', 'Unclassified / Unclassified'],
    ...over,
  } as unknown as Dashboard
}

function outcome(over: Partial<FieldOutcome> = {}): FieldOutcome {
  return { applicable: true, reason: null, before: null, after: null, changed: false, derived: false, manual_replacement: false, skipped_fill_missing: false, ...over }
}

function preview(anchor: string, fields: Partial<Record<FieldName, FieldOutcome>>, commands: ClassificationPreview['commands']): ClassificationPreview {
  const names: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
  const row = { anchor, currency: 'SGD', bucket: 'spending', fields: Object.fromEntries(names.map(n => [n, fields[n] ?? outcome()])) as Record<FieldName, FieldOutcome> }
  return { generation: 'g1', changed_transactions: commands.length ? 1 : 0, command_count: commands.length, commands, by_currency: [], fields: {} as ClassificationPreview['fields'], rows: [row] }
}

function mockApi(opts: {
  categories?: Array<{ key: string; archived: boolean }>
  onPreview?: (body: unknown) => ClassificationPreview
  onBatch?: (body: unknown) => { status: string }
  onDerive?: (body: unknown) => { generation: string; anchors: string[]; options: Array<{ label: string; match: Record<string, unknown> }>; unavailable_reason: string | null }
  onCategoryCompile?: (body: unknown) => unknown
}) {
  apiMock.mockImplementation((path: string, body?: unknown) => {
    if (path === 'categories') return Promise.resolve({ categories: opts.categories ?? [] })
    if (path === 'reporting/workspaces/classification/preview') return opts.onPreview ? Promise.resolve(opts.onPreview(body)) : Promise.reject(new Error('unexpected preview call'))
    if (path === 'batch') return opts.onBatch ? Promise.resolve(opts.onBatch(body)) : Promise.reject(new Error('unexpected batch call'))
    if (path === 'categorization/derive-rule') return opts.onDerive ? Promise.resolve(opts.onDerive(body)) : Promise.reject(new Error('unexpected derive call'))
    if (path === 'reporting/workspaces/rules/compile') return opts.onCategoryCompile ? Promise.resolve(opts.onCategoryCompile(body)) : Promise.reject(new Error('unexpected category compile call'))
    return Promise.reject(new Error(`unmocked path ${path}`))
  })
}

async function pickCategory(name: string) {
  const input = screen.getByLabelText('Category')
  fireEvent.change(input, { target: { value: name } })
  fireEvent.click(await screen.findByText(name))
}

afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear() })

describe('EntryDialog — grocery trio acceptance case', () => {
  it('selects Groceries, Variable routine and Essential together and saves them in one reviewed batch', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { category?: { intent: string; value: string }; cost_behavior?: { intent: string; value: string }; need?: { intent: string; value: string }; reason: string }
        expect(draft.category).toEqual({ intent: 'set', value: 'Food & Dining / Groceries' })
        expect(draft.cost_behavior).toEqual({ intent: 'set', value: 'variable_routine' })
        expect(draft.need).toEqual({ intent: 'set', value: 'essential' })
        expect(draft.reason).toBe('Weekly groceries')
        return preview('a1', {
          category: outcome({ before: 'Unclassified / Unclassified', after: 'Food & Dining / Groceries', changed: true }),
          cost_behavior: outcome({ before: 'unresolved', after: 'variable_routine', changed: true }),
          need: outcome({ before: 'unresolved', after: 'essential', changed: true }),
        }, [
          { kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: 'Weekly groceries' } },
          { kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: { cost_behavior: 'variable_routine', need: 'essential', reason: 'Weekly groceries' } },
        ])
      },
      onBatch: body => {
        const batch = body as { items: unknown[]; reason: string }
        expect(batch.items).toHaveLength(2)
        expect(batch.reason).toBe('Weekly groceries')
        return { status: 'applied' }
      },
    })
    const onSaved = vi.fn()
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={onSaved} />)

    await pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Spending need'), { target: { value: 'essential' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Weekly groceries' } })

    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
    expect(screen.getByRole('status').textContent).toContain('Category: Unclassified → Groceries')
    expect(screen.getByRole('status').textContent).toContain('Spending pattern: Unresolved → Variable routine')
    expect(screen.getByRole('status').textContent).toContain('Spending need: Unresolved → Essential')

    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('changes pattern and need to an individual exception without touching an unchanged category', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { category?: unknown; cost_behavior?: { intent: string; value: string }; need?: { intent: string; value: string } }
        expect(draft.category).toBeUndefined()
        expect(draft.cost_behavior).toEqual({ intent: 'set', value: 'irregular' })
        expect(draft.need).toEqual({ intent: 'set', value: 'discretionary' })
        return preview('a1', {
          cost_behavior: outcome({ before: 'variable_routine', after: 'irregular', changed: true }),
          need: outcome({ before: 'essential', after: 'discretionary', changed: true }),
        }, [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: { cost_behavior: 'irregular', need: 'discretionary', reason: 'One-off splurge' } }])
      },
      onBatch: () => ({ status: 'applied' }),
    })
    const onSaved = vi.fn()
    render(<EntryDialog
      entry={entry({ category: 'Food & Dining / Groceries', basis: 'override', cost_behavior: 'variable_routine', cost_behavior_basis: 'manual', need: 'essential', need_basis: 'manual' })}
      data={dashboard()} onClose={vi.fn()} onSaved={onSaved}
    />)

    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'irregular' } })
    fireEvent.change(screen.getByLabelText('Spending need'), { target: { value: 'discretionary' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'One-off splurge' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('changing only the category never sends a role edit alongside it', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { category: { intent: string; value: string }; cost_behavior?: unknown; need?: unknown }
        expect(draft.category).toEqual({ intent: 'set', value: 'Lifestyle / Books' })
        expect(draft.cost_behavior).toBeUndefined()
        expect(draft.need).toBeUndefined()
        return preview('a1', { category: outcome({ before: 'Food & Dining / Groceries', after: 'Lifestyle / Books', changed: true }) },
          [{ kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Lifestyle / Books', reason: '' } }])
      },
    })
    render(<EntryDialog
      entry={entry({ category: 'Food & Dining / Groceries', basis: 'rule', cost_behavior: 'variable_routine', need: 'essential' })}
      data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()}
    />)
    await pickCategory('Lifestyle / Books')
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
  })
})

describe('EntryDialog — dirty state and identity', () => {
  it('closes an untouched dialog without asking to discard anything', async () => {
    mockApi({})
    const confirmSpy = vi.spyOn(window, 'confirm')
    const onClose = vi.fn()
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={onClose} onSaved={vi.fn()} />)
    await screen.findByLabelText(/Reason/)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('asks to discard a changed draft before closing, and respects a decline', async () => {
    mockApi({})
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const onClose = vi.fn()
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={onClose} onSaved={vi.fn()} />)
    await screen.findByLabelText(/Reason/)
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'a note' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(confirmSpy).toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('never carries a draft from one transaction into a freshly opened one', async () => {
    mockApi({})
    const { rerender } = render(<EntryDialog key="a1" entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    await screen.findByLabelText(/Reason/)
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'draft for a1' } })
    expect(screen.getByLabelText(/Reason/)).toHaveValue('draft for a1')

    rerender(<EntryDialog key="a2" entry={entry({ anchor: 'a2', id: 'e2', merchant: 'Bookshop' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    await screen.findByText('Bookshop')
    expect(screen.getByLabelText(/Reason/)).toHaveValue('')
  })
})

describe('EntryDialog — applicability', () => {
  it('offers nothing editable for a confirmed-relationship internal row', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ bucket: 'internal', linked: true, category: 'Internal / Transfer' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText(/linked relationship owns this movement/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Review changes/ })).not.toBeInTheDocument()
  })

  it('keeps incoming type editable for an unlinked self-transfer row while hiding category', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ bucket: 'internal', linked: false, classification: 'self_transfer', category: 'Internal / Transfer' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByLabelText('Incoming type')).toBeInTheDocument()
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
  })

  it('offers nothing editable for a movement a personal debt fully explains, and says where to change it', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ bucket: 'debt', debt_allocation: 'full', review: false })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText(/fully explained by debt allocations/)).toBeInTheDocument()
    expect(screen.getByText(/Debts page/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Spending pattern')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Review changes/ })).not.toBeInTheDocument()
  })

  it('keeps a partly debt-allocated spending row editable and says the classification covers the rest', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ debt_allocation: 'partial' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText(/classification below covers the rest/)).toBeInTheDocument()
    expect(screen.getByLabelText('Category')).toBeInTheDocument()
  })

  it('does not offer editing for an excluded (inactive) row', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ bucket: 'excluded', eligible: false })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText(/not active in the current selection scope/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
  })
})

describe('EntryDialog — classification presets (Step 6)', () => {
  it('saves a grocery preset, survives a reload, applies to a transaction, and still allows a per-transaction exception', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { category?: { value: string }; cost_behavior?: { value: string }; need?: { value: string } }
        expect(draft.category).toEqual({ intent: 'set', value: 'Food & Dining / Groceries' })
        expect(draft.cost_behavior).toEqual({ intent: 'set', value: 'variable_routine' })
        expect(draft.need).toEqual({ intent: 'set', value: 'essential' })
        return preview('a1', {
          category: outcome({ before: 'Unclassified / Unclassified', after: 'Food & Dining / Groceries', changed: true }),
          cost_behavior: outcome({ before: 'unresolved', after: 'variable_routine', changed: true }),
          need: outcome({ before: 'unresolved', after: 'essential', changed: true }),
        }, [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} }])
      },
      onBatch: () => ({ status: 'applied' }),
    })

    // Build and save "Everyday groceries" on one transaction.
    const { unmount } = render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    await pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Spending need'), { target: { value: 'essential' } })
    fireEvent.change(screen.getByLabelText('New preset name'), { target: { value: 'Everyday groceries' } })
    fireEvent.click(screen.getByText('Save as preset'))
    expect(screen.getByRole('button', { name: 'Everyday groceries' })).toBeInTheDocument()
    unmount()

    // A fresh mount ("reload") on a different, unclassified transaction still sees it.
    const onSaved = vi.fn()
    render(<EntryDialog entry={entry({ anchor: 'a1', merchant: 'Cold Storage' })} data={dashboard()} onClose={vi.fn()} onSaved={onSaved} />)
    expect(screen.getByRole('button', { name: 'Everyday groceries' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Review changes/ })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Everyday groceries' }))
    expect(screen.getByLabelText('Category')).toHaveValue('Food & Dining / Groceries')
    expect(screen.getByLabelText('Spending pattern')).toHaveValue('variable_routine')
    expect(screen.getByLabelText('Spending need')).toHaveValue('essential')

    // Applying a preset never fills the reason, and a required reason still gates saving.
    expect(screen.getByRole('button', { name: /Review changes/ })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Weekly groceries' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('lets one transaction override a single field after applying a preset, without touching the others', async () => {
    mockApi({
      onPreview: body => {
        const draft = body as { category?: unknown; cost_behavior?: { value: string }; need?: { value: string } }
        expect(draft.category).toEqual({ intent: 'set', value: 'Food & Dining / Groceries' })
        expect(draft.cost_behavior).toEqual({ intent: 'set', value: 'irregular' })
        expect(draft.need).toEqual({ intent: 'set', value: 'essential' })
        return preview('a1', {
          category: outcome({ before: 'Unclassified / Unclassified', after: 'Food & Dining / Groceries', changed: true }),
          cost_behavior: outcome({ before: 'unresolved', after: 'irregular', changed: true }),
          need: outcome({ before: 'unresolved', after: 'essential', changed: true }),
        }, [{ kind: 'ENTRY_ROLE_SET', anchors: { entry: 'a1' }, payload: {} }])
      },
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('New preset name'), { target: { value: 'x' } })
    // Nothing chosen yet: cannot save an empty preset.
    expect(screen.getByText('Save as preset').closest('button')).toBeDisabled()

    await pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('Spending need'), { target: { value: 'essential' } })
    fireEvent.change(screen.getByLabelText('New preset name'), { target: { value: 'Everyday groceries' } })
    fireEvent.click(screen.getByText('Save as preset'))

    // The exception: keep category and need, override just the pattern.
    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'irregular' } })
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'One-off bulk buy' } })
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
  })

  it('drops a since-deleted category from an applied preset without silently remapping it', async () => {
    mockApi({})
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    await pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText('Spending pattern'), { target: { value: 'variable_routine' } })
    fireEvent.change(screen.getByLabelText('New preset name'), { target: { value: 'Stale' } })
    fireEvent.click(screen.getByText('Save as preset'))

    // A fresh entry, viewed against a ledger where that category no longer exists.
    cleanup()
    const shrunkDashboard = dashboard({ categories: ['Lifestyle / Books', 'Unclassified / Unclassified'] })
    render(<EntryDialog entry={entry({ anchor: 'a2', merchant: 'Somewhere else' })} data={shrunkDashboard} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Stale' }))
    await screen.findByText(/archived or no longer exists/)
    expect(screen.getByLabelText('Category')).toHaveValue('')
    expect(screen.getByLabelText('Spending pattern')).toHaveValue('variable_routine')
  })
})

describe('EntryDialog — keyboard operation', () => {
  it('lets a keyboard-only user choose a category with the arrow keys and Enter', async () => {
    mockApi({
      onPreview: () => preview('a1', { category: outcome({ before: null, after: 'Food & Dining / Groceries', changed: true }) },
        [{ kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: '' } }]),
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    const input = screen.getByLabelText('Category')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'Groceries' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('Food & Dining / Groceries')
    fireEvent.click(screen.getByRole('button', { name: /Review changes/ }))
    await screen.findByText(/Ready to save/)
  })
})

describe('EntryDialog — optional reusable rule (Step 7)', () => {
  it('offers a rule for the field the user actually chose, targeting only this transaction unless a predicate is confirmed', async () => {
    mockApi({
      onDerive: body => {
        expect(body).toEqual({ generation: 'g1', anchors: ['a1'] })
        return { generation: 'g1', anchors: ['a1'], options: [{ label: 'All transactions from this merchant', match: { merchant_canonical: 'Acme Grocer' } }], unavailable_reason: null }
      },
      onCategoryCompile: body => {
        expect(body).toEqual({ match: { merchant_canonical: 'Acme Grocer' }, category: 'Food & Dining / Groceries', reason: 'Weekly shop' })
        return {
          command: { kind: 'CATEGORY_RULE_SET', anchors: { rule_id: 'rule-1' }, payload: {} },
          impact: { predicate_matches: 4, effective_assignments: 4, newly_categorized: 2, recategorized: 0, protected_by_override: 0, direction_conflict: 0 },
        }
      },
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    await pickCategory('Food & Dining / Groceries')
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Weekly shop' } })

    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    fireEvent.click(screen.getByRole('button', { name: /Check for a reusable rule/ }))
    await screen.findByText('All transactions from this merchant')
    fireEvent.click(screen.getByText('All transactions from this merchant'))
    fireEvent.click(screen.getByLabelText('Reusable category rule', { exact: false }))
    fireEvent.click(screen.getByRole('button', { name: /Preview rule/ }))
    await screen.findByText('Category rule impact')
  })

  it('offers no rule for a field the user left unchanged, even if the transaction already has a value there', async () => {
    mockApi({})
    render(<EntryDialog
      entry={entry({ category: 'Food & Dining / Groceries', basis: 'rule', cost_behavior: 'variable_routine', need: 'essential' })}
      data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()}
    />)
    fireEvent.click(screen.getByRole('button', { name: /Also save a reusable rule/ }))
    expect(screen.getByText(/Choose a category, spending pattern, or spending need above/)).toBeInTheDocument()
  })

  it('is not offered at all for a non-editable (excluded) transaction', () => {
    mockApi({})
    render(<EntryDialog entry={entry({ bucket: 'excluded', eligible: false })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Also save a reusable rule/ })).not.toBeInTheDocument()
  })
})
