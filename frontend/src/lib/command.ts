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
