import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError, type Dashboard, type Entry } from '@/lib/api'
import { OwnerShareForm } from './OwnerShareForm'
import { EntryDialog } from '@/features/classification/SingleTransactionEditor'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (original) => ({ ...await original<typeof import('@/lib/api')>(), api: (path: string, body?: unknown) => apiMock(path, body) }))
const categories = ['Food & Dining / Groceries', 'Food & Dining / Dining Out']
const source = { generation: 'g1', entry_anchor: 'bill', currency: 'USD', amount: '120.00', available_amount: '40.00', unassigned_amount: '0.00',
  owner_share: { expense_amount: '40.00', expense_category: categories[0], reason: 'Original split' } as { expense_amount: string; expense_category: string; reason: string } | null }
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear() })
function setup(value = source) {
  apiMock.mockImplementation((path: string) => Promise.resolve(path.startsWith('debts/entry-residual?') ? value : path === 'categories' ? { categories: [] } : { status: 'APPLIED' }))
  const saved = vi.fn()
  render(<OwnerShareForm anchor="bill" generation="g1" categories={categories} onSaved={saved} onCancel={vi.fn()}/> )
  return saved
}
const calls = (path: string) => apiMock.mock.calls.filter(([p]) => p === path)
async function checkShare() {
  fireEvent.change(screen.getByLabelText('Reason for this change'), { target: { value: 'Correct the share' } })
  fireEvent.click(screen.getByRole('button', { name: 'Check share' }))
  await screen.findByRole('button', { name: 'Save share' })
}
describe('OwnerShareForm', () => {
  it('checks then saves the native share with the same request id and no debt command', async () => {
    const saved = setup()
    await screen.findByDisplayValue('40.00')
    fireEvent.change(screen.getByLabelText('Your share (USD)'), { target: { value: '30.005' } })
    await checkShare()
    expect(calls('commands')).toHaveLength(0)
    const body = calls('preview')[0][1]
    expect(body).toMatchObject({ kind: 'ENTRY_RESIDUAL_SET', expected_generation: 'g1', anchors: { entry: 'bill' }, payload: { expense_amount: '30.005', expense_category: categories[0], reason: 'Correct the share' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save share' }))
    await waitFor(() => expect(saved).toHaveBeenCalledWith('APPLIED'))
    expect(calls('commands')[0][1]).toEqual(body)
  })
  it('previews removal and explains that the released amount returns to ordinary classification', async () => {
    setup()
    await screen.findByDisplayValue('40.00')
    fireEvent.click(screen.getByLabelText('Remove my recorded share'))
    expect(screen.getByText(/returns to the transaction’s ordinary classification/)).toBeInTheDocument()
    await checkShare()
    expect(calls('preview')[0][1]).toMatchObject({ kind: 'ENTRY_RESIDUAL_RETIRE', anchors: { entry: 'bill' }, payload: { reason: 'Correct the share' } })
    expect(screen.queryByLabelText('Your share (USD)')).not.toBeInTheDocument()
  })
  it('leaves the limit to the server and shows its refusal with the draft kept', async () => {
    setup({ ...source, available_amount: '9007199254740992.01' })
    await screen.findByDisplayValue('40.00')
    apiMock.mockImplementation((path: string) => path === 'preview'
      ? Promise.reject(new ApiError('Your share is more than the 9007199254740992.01 other allocations leave of this transaction', 422))
      : Promise.resolve({ status: 'APPLIED' }))
    fireEvent.change(screen.getByLabelText('Your share (USD)'), { target: { value: '9007199254740992.02' } })
    fireEvent.change(screen.getByLabelText('Reason for this change'), { target: { value: 'Too much' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check share' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('more than the 9007199254740992.01')
    expect(calls('preview')[0][1]).toMatchObject({ payload: { expense_amount: '9007199254740992.02' } })
    expect(screen.getByLabelText('Your share (USD)')).toHaveValue('9007199254740992.02')
    expect(calls('commands')).toHaveLength(0)
  })
  it('preserves refused drafts and freezes uncertain saves for a retry with the same id', async () => {
    const saved = setup()
    await screen.findByDisplayValue('40.00')
    apiMock.mockImplementation((path: string) => path === 'preview' ? Promise.reject(new ApiError('The ledger changed', 409)) : Promise.resolve({ status: 'APPLIED' }))
    fireEvent.change(screen.getByLabelText('Reason for this change'), { target: { value: 'Keep this draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check share' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The ledger changed')
    expect(screen.getByLabelText('Your share (USD)')).toHaveValue('40.00')
    apiMock.mockImplementation((path: string) => path === 'commands' ? Promise.reject(new Error('Network interrupted')) : Promise.resolve({}))
    fireEvent.click(screen.getByRole('button', { name: 'Check share' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save share' }))
    await screen.findByRole('button', { name: 'Retry save' })
    const first = calls('commands')[0][1]
    expect(screen.getByLabelText('Your share (USD)')).toBeDisabled()
    apiMock.mockResolvedValue({ status: 'APPLIED' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    expect(calls('commands')[1][1]).toEqual(first)
  })
  it('does not infer an expense share from the unassigned amount', async () => {
    setup({ ...source, owner_share: null, unassigned_amount: '40.00' })
    await screen.findByLabelText('Your share (USD)')
    expect(screen.getByLabelText('Your share (USD)')).toHaveValue('')
    expect(screen.queryByLabelText('Remove my recorded share')).not.toBeInTheDocument()
  })
  it('opens for a fully allocated shared bill from the transaction dialog', async () => {
    apiMock.mockImplementation((path: string) => path.startsWith('entry-context?') ? Promise.reject(new Error('not under test'))
      : Promise.resolve(path.startsWith('debts/entry-residual?') ? source : { categories: [] }))
    const entry = { anchor: 'bill', id: 'bill-id', amount: '-120.00', currency: 'USD', fee: '0.00', date: '2026-01-20', precision: 'DAY', merchant: 'Shared meal', category: categories[0], bucket: 'debt', debt_allocation: 'full', basis: 'source', account: 'Card', description: 'Shared meal', cost_behavior: 'N/A', need: 'N/A', role_review_state: 'N/A' } as Entry
    render(<EntryDialog entry={entry} data={{ generation: 'g1', freshness: 'current', categories } as Dashboard} onClose={vi.fn()} onSaved={vi.fn()}/> )
    fireEvent.click(screen.getByRole('button', { name: 'Review my expense share' }))
    expect(await screen.findByDisplayValue('40.00')).toBeInTheDocument()
    expect(screen.getByText(/your expense share keeps its own category/)).toBeInTheDocument()
  })
})
