import { useCallback, useEffect, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { dateLabel } from '@/lib/format'
import { useBatchCommand, type BatchItem } from '@/lib/command'
import { countryName, countrySet, loadCountries, loadEntryContext, tripAssign, tripAssignRetire, type EntryContext } from './api'

const KEEP = ''
const UNKNOWN = 'unknown'
const FROM_SOURCE = 'source'
const NO_TRIP = 'none'
const AUTOMATIC = 'automatic'

const ASSIGNMENT: Record<string, string> = { automatic: 'joined automatically', manual: 'chosen by hand', excluded: 'excluded from every trip by hand', ambiguous: 'inside more than one trip; choose one', none: 'not in a trip' }

/**
 * One transaction's country and trip, set from the transaction editor with the
 * same decisions the Trips page records. Country is never guessed: it is what
 * the user says, an explicit unknown, or whatever the source said.
 */
export function EntryContextSection({ anchor, generation, disabled, onSaved }: {
  anchor: string
  generation: string
  disabled: boolean
  onSaved: (status: string) => Promise<void>
}) {
  const [context, setContext] = useState<EntryContext | null>(null)
  const [codes, setCodes] = useState<string[]>([])
  const [country, setCountry] = useState(KEEP)
  const [trip, setTrip] = useState(KEEP)
  const [reason, setReason] = useState('')
  const [reviewed, setReviewed] = useState<BatchItem[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const batch = useBatchCommand(generation, async status => { setCountry(KEEP); setTrip(KEEP); setReason(''); setReviewed(null); await onSaved(status) })

  const load = useCallback(() => {
    setLoadError('')
    loadEntryContext(anchor).then(setContext).catch(err => setLoadError((err as Error).message))
  }, [anchor])
  useEffect(() => { load() }, [load, generation])
  useEffect(() => { loadCountries().then(r => setCodes(r.codes)).catch(() => {}) }, [])

  if (loadError) return <section className="entry-context"><h3>Trip and country</h3><p className="form-error" role="alert">{loadError}</p></section>
  if (!context) return null

  const tripName = (id: string | null) => context.trips.find(t => t.trip_id === id)?.name ?? 'a removed trip'
  const offersTrip = context.counted_as_cost || context.assignment === 'manual' || context.assignment === 'excluded'
  const items: BatchItem[] = []
  const why = reason.trim()
  if (country === UNKNOWN) items.push(countrySet(anchor, null, why))
  else if (country === FROM_SOURCE) items.push({ kind: 'ENTRY_COUNTRY_RETIRE', anchors: { entry: anchor }, payload: { reason: why } })
  else if (country !== KEEP) items.push(countrySet(anchor, country, why))
  if (trip === NO_TRIP) items.push(tripAssign(anchor, null, why))
  else if (trip === AUTOMATIC) items.push(tripAssignRetire(anchor, why))
  else if (trip !== KEEP) items.push(tripAssign(anchor, trip, why))

  function changed() { setReviewed(null); batch.invalidate() }

  const countryNow = context.country ? `${countryName(context.country)} (${context.country_basis === 'operator' ? 'set by you' : 'from the source'})`
    : context.country_basis === 'operator' ? 'unknown (set by you)' : 'unknown'
  const tripNow = context.trip ? `${tripName(context.trip)}, ${ASSIGNMENT[context.assignment]}` : ASSIGNMENT[context.assignment]

  return <form className="entry-context" aria-label="Trip and country" onSubmit={event => { event.preventDefault(); if (!reviewed) setReviewed(items); else void batch.submit(reviewed, why) }}>
    <h3>Trip and country</h3>
    <p className="muted-note">Country: {countryNow}. Trip: {tripNow}.{!context.counted_as_cost && ' This transaction is not spending or a refund, so it never counts toward a trip.'}</p>
    <label>Country
      <select value={country} disabled={disabled || batch.busy} onChange={event => { setCountry(event.target.value); changed() }}>
        <option value={KEEP}>Keep current country</option>
        {codes.map(code => <option key={code} value={code}>{code} · {countryName(code)}</option>)}
        <option value={UNKNOWN}>Unknown (explicit)</option>
        {context.country_basis === 'operator' && <option value={FROM_SOURCE}>Clear my choice</option>}
      </select>
    </label>
    {offersTrip && <label>Trip
      <select value={trip} disabled={disabled || batch.busy} onChange={event => { setTrip(event.target.value); changed() }}>
        <option value={KEEP}>Keep current trip</option>
        {context.counted_as_cost && context.trips.map(t => <option key={t.trip_id} value={t.trip_id}>
          {t.name} · {dateLabel(t.start_on)} – {dateLabel(t.end_on)}{t.in_window ? '' : ' (outside its dates)'}
        </option>)}
        <option value={NO_TRIP}>Not part of any trip</option>
        {(context.assignment === 'manual' || context.assignment === 'excluded') && <option value={AUTOMATIC}>Let dates and country decide</option>}
      </select>
    </label>}
    <label>Why this trip or country
      <Input maxLength={2000} disabled={disabled || batch.busy} value={reason} placeholder="For your history"
        onChange={event => { setReason(event.target.value); changed() }}/>
    </label>
    {reviewed && <div className="preview" role="status"><p>{reviewed.map(item => describe(item, tripName)).join(' ')}</p></div>}
    {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
    <div className="row-actions">
      <Button type="submit" variant="outline" disabled={disabled || batch.busy || items.length === 0 || !why}>
        {batch.busy ? 'Working…' : reviewed ? (batch.submitted && batch.error ? 'Retry same save' : 'Save trip and country') : 'Review trip and country'}
        {!batch.busy && <ArrowRight size={15}/>}
      </Button>
    </div>
  </form>
}

function describe(item: BatchItem, tripName: (id: string | null) => string): string {
  if (item.kind === 'ENTRY_COUNTRY_SET') return item.payload.country ? `Sets the country to ${countryName(String(item.payload.country))}.` : 'Marks the country as unknown.'
  if (item.kind === 'ENTRY_COUNTRY_RETIRE') return 'Clears your country choice.'
  if (item.kind === 'ENTRY_TRIP_RETIRE') return 'Lets dates and country decide the trip.'
  return item.payload.trip ? `Assigns it to ${tripName(String(item.payload.trip))}.` : 'Keeps it out of every trip.'
}
