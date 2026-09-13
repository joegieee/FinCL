import { useRef, useState } from 'react'
import { api, ApiError } from './api'

export type CommandBody = {
  request_id: string
  expected_generation: string
  kind: string
  anchors: Record<string, string>
  payload: Record<string, unknown>
}

type Draft = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }

/**
 * Two-step ledger write: first call previews, second call commits the exact same
 * request id. A 409/422 clears the preview so the next attempt re-previews; any
 * other failure keeps the preview so a retry re-sends the same id safely. The
 * caller's own form fields are the draft and are never touched here.
 */
export function useLedgerCommand(generation: string, onSaved: (status: string) => void | Promise<void>) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [previewed, setPreviewed] = useState<CommandBody | null>(null)
  const [attempted, setAttempted] = useState(false)
  const inFlight = useRef(false)

  function reset() {
    if (!attempted) { setPreviewed(null); setError('') }
  }

  async function act(draft: Draft) {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true); setError('')
    try {
      if (!previewed) {
        const body: CommandBody = { ...draft, request_id: crypto.randomUUID(), expected_generation: generation }
        await api('preview', body)
        setPreviewed(body)
      } else {
        setAttempted(true)
        const result = await api<{ status: string }>('commands', previewed)
        await onSaved(result.status)
        setPreviewed(null); setAttempted(false)
      }
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && [409, 422].includes(e.status)) { setPreviewed(null); setAttempted(false) }
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  return { busy, error, previewed, attempted, reset, act }
}

export type BatchItem = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }
export type BatchBody = {
  request_id: string
  expected_generation: string
  reason: string
  items: BatchItem[]
}

/**
 * Retry-safe lifecycle for an already-previewed batch (e.g. the exact
 * `commands` a `classification/preview` call returned). Unlike the two-step
 * single-command lifecycle above, a batch's server-side preview already
 * happened out of band; this hook only owns the one property every direct
 * batch-save caller in the codebase currently lacks (PLAN-unified-
 * transaction-classification.md Step 0/1): the submitted request id and body
 * are frozen on the first attempt and reused verbatim on every retry, so an
 * uncertain network failure can never turn into a duplicate write. A 409/422
 * is definitive and clears the frozen body so the next attempt re-submits a
 * fresh draft; any other failure keeps it so a retry reuses the same id.
 */
export function useBatchCommand(generation: string, onSaved: (status: string) => void | Promise<void>) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [submitted, setSubmitted] = useState<BatchBody | null>(null)
  const inFlight = useRef(false)

  /** Call when the draft, selection, policy, preset, or generation changes so
   * a stale frozen body is never resubmitted after those are no longer what
   * was reviewed. A body already sent (attempted) is left alone. */
  function invalidate() {
    if (!busy) { setSubmitted(null); setError('') }
  }

  async function submit(items: BatchItem[], reason: string) {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true); setError('')
    try {
      const body: BatchBody = submitted ?? { request_id: crypto.randomUUID(), expected_generation: generation, reason, items }
      if (!submitted) setSubmitted(body)
      const result = await api<{ status: string }>('batch', body)
      await onSaved(result.status)
      setSubmitted(null)
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && [409, 422].includes(e.status)) setSubmitted(null)
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  /** Resubmit the exact frozen request after an uncertain (non-409/422) failure. */
  async function retry() {
    if (submitted) await submit(submitted.items, submitted.reason)
  }

  return { busy, error, submitted, invalidate, submit, retry }
}
