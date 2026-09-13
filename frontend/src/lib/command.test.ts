import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({ api: (path: string, body?: unknown) => apiMock(path, body), ApiError: class ApiError extends Error { status = 400; constructor(message: string, status: number) { super(message); this.status = status } } }))
const { useBatchCommand } = await import('./command')
const { ApiError } = await import('./api')

afterEach(() => { vi.clearAllMocks() })

const items = [{ kind: 'CATEGORY_OVERRIDE', anchors: { entry: 'a1' }, payload: { category: 'Food & Dining / Groceries', reason: '' } }]

describe('useBatchCommand', () => {
  it('freezes one request id and body across a retry after an uncertain failure', async () => {
    const onSaved = vi.fn()
    const { result } = renderHook(() => useBatchCommand('g1', onSaved))
    apiMock.mockRejectedValueOnce(new Error('network dropped'))
    await act(async () => { await result.current.submit(items, 'reclassify') })
    expect(result.current.error).toBe('network dropped')
    const frozen = result.current.submitted
    expect(frozen).not.toBeNull()

    apiMock.mockResolvedValueOnce({ status: 'applied' })
    await act(async () => { await result.current.retry() })
    expect(apiMock).toHaveBeenNthCalledWith(2, 'batch', frozen)
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(result.current.submitted).toBeNull()
  })

  it('clears the frozen body on a definitive 409/422 so the next attempt re-submits fresh', async () => {
    const onSaved = vi.fn()
    const { result } = renderHook(() => useBatchCommand('g1', onSaved))
    apiMock.mockRejectedValueOnce(new ApiError('STALE_GENERATION', 409))
    await act(async () => { await result.current.submit(items, 'reclassify') })
    expect(result.current.submitted).toBeNull()
  })

  it('a double-click while in flight does not issue a second request', async () => {
    const onSaved = vi.fn()
    const { result } = renderHook(() => useBatchCommand('g1', onSaved))
    let resolve!: (value: { status: string }) => void
    apiMock.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    // Deliberately not awaited: this call must overlap the second submit() below.
    const first = act(async () => { await result.current.submit(items, 'reclassify') })
    await act(async () => { await result.current.submit(items, 'reclassify') })
    expect(apiMock).toHaveBeenCalledTimes(1)
    resolve({ status: 'applied' })
    await first
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('invalidate drops a frozen body once the draft has changed and nothing is in flight', async () => {
    const onSaved = vi.fn()
    const { result } = renderHook(() => useBatchCommand('g1', onSaved))
    apiMock.mockRejectedValueOnce(new Error('network dropped'))
    await act(async () => { await result.current.submit(items, 'reclassify') })
    expect(result.current.submitted).not.toBeNull()
    act(() => { result.current.invalidate() })
    expect(result.current.submitted).toBeNull()
    expect(result.current.error).toBe('')
  })

  it('a successful commit resolves recorded_refresh_pending without a second write', async () => {
    const onSaved = vi.fn()
    const { result } = renderHook(() => useBatchCommand('g1', onSaved))
    apiMock.mockResolvedValueOnce({ status: 'recorded_refresh_pending' })
    await act(async () => { await result.current.submit(items, 'reclassify') })
    expect(onSaved).toHaveBeenCalledWith('recorded_refresh_pending')
    expect(result.current.submitted).toBeNull()
    expect(apiMock).toHaveBeenCalledTimes(1)
  })
})
