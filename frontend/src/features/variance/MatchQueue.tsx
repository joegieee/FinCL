import { useState } from 'react'
import { Link2, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ApiError, money } from '@/lib/api'
import { recordMatch, type MatchCandidate, type MatchSuggestion } from './api'

// A long gap in coverage can leave hundreds of events waiting, which rendered
// as one page tens of thousands of pixels tall; reveal the queue in steps.
const QUEUE_STEP = 10

/**
 * The matching queue: bind real cash to the events a forecast issued.
 *
 * Until something binds a movement to an event, an event that was answered late
 * is indistinguishable from one that was never answered at all, and no
 * realization rate can exist — which is why this workflow, not the arithmetic
 * behind it, was the missing half of forecast learning.
 *
 * Every candidate is a suggestion and nothing more. The shortlist is ranked by
 * amount difference then distance from the issued window, and both are shown, so
 * a wrong suggestion is visibly wrong rather than quietly authoritative. A match
 * is journalled and can be retired; retiring it returns the event to whatever
 * status the evidence alone supports.
 */
export function MatchQueue({
  suggestions, snapshot, generation, currency, onMatched,
}: {
  suggestions: MatchSuggestion[]
  snapshot: string
  generation: string
  currency: string
  onMatched: (status: string) => void | Promise<void>
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [shown, setShown] = useState(QUEUE_STEP)

  async function bind(suggestion: MatchSuggestion, candidate: MatchCandidate) {
    setBusy(candidate.movement_id); setError('')
    try {
      const result = await recordMatch(generation, snapshot, {
        event_id: suggestion.event_id,
        movement_id: candidate.movement_id,
        matched_on: candidate.on,
        amount: candidate.suggested_amount,
        match_kind: candidate.match_kind,
        reason: `matched ${candidate.label} to ${suggestion.label}`,
      })
      await onMatched(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record that match.')
    } finally {
      setBusy('')
    }
  }

  if (suggestions.length === 0) {
    return (
      <p className="muted-note">
        No issued event is waiting on a match. Every event is either answered, still inside its
        timing window with nothing plausible to bind, or in a period the evidence does not cover.
      </p>
    )
  }

  return (
    <div className="match-queue">
      {error && <p className="debt-event-error" role="alert">{error}</p>}
      {suggestions.slice(0, shown).map(suggestion => (
        <div className="match-group" key={suggestion.event_id}>
          <p className="match-group-title">
            <strong>{suggestion.label}</strong>
            {' '}<Badge variant="outline" className="review-badge">{suggestion.status}</Badge>
            {' '}· {money(suggestion.outstanding, currency)} outstanding
            {' '}· expected {suggestion.window.earliest} → {suggestion.window.latest}
          </p>
          <ul className="issue-list">
            {suggestion.candidates.map(candidate => (
              <li key={candidate.movement_id}>
                <div className="remedy-body">
                  <p>
                    <strong>{candidate.label}</strong> · {candidate.on} · {money(candidate.amount, currency)}
                    {candidate.account && <> · {candidate.account}</>}
                  </p>
                  <p className="muted-note">
                    {candidate.exact_amount
                      ? 'Exact amount'
                      : `${money(candidate.amount_difference, currency)} from the outstanding amount`}
                    {' · '}
                    {candidate.days_from_window === 0
                      ? 'inside the issued window'
                      : `${candidate.days_from_window} day${candidate.days_from_window === 1 ? '' : 's'} outside the issued window`}
                    {candidate.match_kind === 'partial' && ' · would answer this event in part only'}
                  </p>
                </div>
                <Button
                  variant="outline" size="sm"
                  disabled={busy !== ''}
                  aria-label={`Match ${candidate.label} on ${candidate.on} to ${suggestion.label}`}
                  onClick={() => void bind(suggestion, candidate)}
                >
                  {busy === candidate.movement_id
                    ? <Loader2 size={14} className="spin" />
                    : <Link2 size={14} />}
                  {' '}Match
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {suggestions.length > shown && (
        <div className="match-queue-more">
          <span>Showing {shown} of {suggestions.length} events waiting on a match.</span>
          <Button variant="outline" size="sm" onClick={() => setShown(n => n + QUEUE_STEP)}>
            Show {Math.min(QUEUE_STEP, suggestions.length - shown)} more
          </Button>
        </div>
      )}
      <p className="muted-note">{suggestions[0].rule}.</p>
    </div>
  )
}
