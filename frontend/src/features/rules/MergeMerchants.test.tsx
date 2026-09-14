import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status = 422 },
}))

const { MergeMerchantsDialog } = await import('./MergeMerchants')

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('MergeMerchantsDialog', () => {
  it('loads suggestions for the seed key and lets a conflict block save until resolved', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('merchants/similar')) {
        return Promise.resolve({
          generation: 'g1', merchant_key: 'GRAB*RIDE',
          suggestions: [{ merchant_key: 'GRAB FOOD SG', display_name: null, variants: [], transaction_count: 5, last_seen: '2026-01-01', has_rules: false }],
        })
      }
      if (path === 'categorization/merchant-merge/preview') {
        return Promise.resolve({
          generation: 'g1', sources: ['GRAB FOOD SG'], target: 'GRAB*RIDE', patterns: [{ exact: 'GRAB FOOD SG' }],
          commands: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'GRAB*RIDE' }, payload: {} }],
          conflicts: [{ kind: 'category', candidates: [{ from_merchant: 'GRAB*RIDE', value: 'Transport / Taxi & Ride-hail' }, { from_merchant: 'GRAB FOOD SG', value: 'Food & Dining / Groceries' }] }],
          affected_transactions: 8, dependent_rules: 2,
        })
      }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    const onSaved = vi.fn()
    render(<MergeMerchantsDialog generation="g1" seedKeys={['GRAB*RIDE']} onClose={() => {}} onSaved={onSaved} />)
    expect(await screen.findByText(/GRAB FOOD SG/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /GRAB FOOD SG/ }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'same business' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview merge' }))

    expect(await screen.findByText(/2 rules rewritten/)).toBeInTheDocument()
    expect(screen.getByText(/Resolve every conflict above/)).toBeInTheDocument()
    const saveButton = screen.getByRole('button', { name: 'Save merge' })
    expect(saveButton).toBeDisabled()

    fireEvent.click(screen.getByRole('radio', { name: /GRAB FOOD SG/ }))
    // Changing a resolution invalidates the frozen preview; re-preview with it applied.
    apiMock.mockResolvedValueOnce({
      generation: 'g1', sources: ['GRAB FOOD SG'], target: 'GRAB*RIDE', patterns: [{ exact: 'GRAB FOOD SG' }],
      commands: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'GRAB*RIDE' }, payload: {} }],
      conflicts: [], affected_transactions: 8, dependent_rules: 2,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview merge' }))
    expect(await screen.findByText(/Ready to merge/)).toBeInTheDocument()

    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(screen.getByRole('button', { name: 'Save merge' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(apiMock).toHaveBeenCalledWith('batch', expect.objectContaining({
      items: [{ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: 'GRAB*RIDE' }, payload: {} }],
    }))
  })

  it('searches for more merchants to add to the selection', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('merchants/similar')) return Promise.resolve({ generation: 'g1', merchant_key: 'KFC', suggestions: [] })
      if (path.startsWith('merchants?')) {
        return Promise.resolve({
          generation: 'g1',
          merchants: [{ merchant_key: 'KFC EXPRESS', display_name: null, variants: [], transaction_count: 2, last_seen: null, has_rules: false }],
        })
      }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<MergeMerchantsDialog generation="g1" seedKeys={['KFC']} onClose={() => {}} onSaved={() => {}} />)
    await screen.findByText('KFC')
    fireEvent.change(screen.getByLabelText('Search for more'), { target: { value: 'kfc' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText(/KFC EXPRESS/)).toBeInTheDocument()
    expect(apiMock).toHaveBeenCalledWith('merchants?generation=g1&q=kfc&limit=20', undefined)
  })
})
