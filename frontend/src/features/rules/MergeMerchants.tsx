import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useBatchCommand } from '@/lib/command'
import { merchantMergePreview, searchMerchants, similarMerchants, type MerchantMergePreview, type MerchantRow } from './api'

/**
 * "Same merchant as..." / multi-select "Merge" (PLAN-categorization-workflow.md
 * Step 4). One dialog serves both entry points: the categorization inbox opens
 * it with one seed key (the group being reviewed) and offers suggestions plus
 * search to add more; the Rules page opens it with an already multi-selected
 * set of merchant keys and skips straight to naming a target. Either way the
 * flow ends the same way: preview, resolve any conflicts, save one batch.
 */
export function MergeMerchantsDialog({ generation, seedKeys, seedDisplayName, disabled, onClose, onSaved }: {
  generation: string; seedKeys: string[]; seedDisplayName?: string; disabled?: boolean
  onClose: () => void; onSaved: (status: string) => void | Promise<void>
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(seedKeys))
  const [target, setTarget] = useState(seedKeys[0] ?? '')
  const [displayName, setDisplayName] = useState(seedDisplayName ?? seedKeys[0] ?? '')
  const [reason, setReason] = useState('')
  const [suggestions, setSuggestions] = useState<MerchantRow[] | null>(null)
  const [suggestError, setSuggestError] = useState('')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<MerchantRow[]>([])
  const [searching, setSearching] = useState(false)
  const [resolutions, setResolutions] = useState<Record<string, string>>({})
  const [preview, setPreview] = useState<MerchantMergePreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [error, setError] = useState('')
  const batch = useBatchCommand(generation, async status => { await onSaved(status); onClose() })

  const firstSeedKey = seedKeys[0]
  useEffect(() => {
    if (!firstSeedKey) return
    similarMerchants(generation, firstSeedKey)
      .then(page => setSuggestions(page.suggestions))
      .catch(e => setSuggestError((e as Error).message))
    // Only ever fetched once for the dialog's fixed seed; adding more sources
    // later does not re-query suggestions against every one of them.
  }, [generation, firstSeedKey])

  function resetPreview() { setPreview(null); batch.invalidate(); setError('') }
  function toggle(key: string) {
    setSelected(current => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
    resetPreview()
  }
  function chooseTarget(key: string) {
    setTarget(key)
    resetPreview()
  }
  async function runSearch() {
    if (!query.trim() || searching) return
    setSearching(true); setError('')
    try { setResults((await searchMerchants(generation, query.trim())).merchants) }
    catch (e) { setError((e as Error).message) } finally { setSearching(false) }
  }
  const sources = [...selected].filter(key => key !== target)
  const canPreview = Boolean(target) && sources.length > 0 && Boolean(reason.trim())
  async function runPreview() {
    if (!canPreview || previewing) return
    setPreviewing(true); setError(''); setPreview(null)
    try {
      setPreview(await merchantMergePreview(generation, sources, target, displayName.trim() || null, reason.trim(), resolutions))
    } catch (e) { setError((e as Error).message) } finally { setPreviewing(false) }
  }
  async function save() {
    if (preview) await batch.submit(preview.commands, reason.trim())
  }
  const unresolved = preview?.conflicts.filter(conflict => !resolutions[conflict.kind]) ?? []

  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="merge-merchants-dialog" aria-label="Merge merchants">
      <DialogHeader>
        <DialogTitle>Merge merchants</DialogTitle>
        <DialogDescription>Mark these statement names as one merchant. Existing rules move to the combined identity; nothing is guessed automatically.</DialogDescription>
      </DialogHeader>
      <fieldset disabled={disabled || batch.busy}>
        <legend>Merchants to combine</legend>
        {[...selected].map(key => <label key={key} className="merge-merchant-row">
          <input type="checkbox" checked disabled={selected.size <= 1} onChange={() => toggle(key)} />
          <span>{key}</span>
          <label className="merge-target-radio"><input type="radio" name="merge-target" checked={target === key} onChange={() => chooseTarget(key)} /> Keep as target</label>
        </label>)}
        {selected.size < 2 && <p className="muted-note">Add at least one more merchant below.</p>}
      </fieldset>
      {suggestError && <p className="form-error" role="alert">{suggestError}</p>}
      {suggestions && suggestions.filter(row => !selected.has(row.merchant_key)).length > 0 && <fieldset disabled={disabled || batch.busy}>
        <legend>Suggested</legend>
        {suggestions.filter(row => !selected.has(row.merchant_key)).map(row => <label key={row.merchant_key} className="merge-merchant-row">
          <input type="checkbox" checked={false} onChange={() => toggle(row.merchant_key)} />
          <span>{row.display_name ? `${row.display_name} (${row.merchant_key})` : row.merchant_key}</span>
          <small>{row.transaction_count} transaction{row.transaction_count === 1 ? '' : 's'}</small>
        </label>)}
      </fieldset>}
      <div className="merge-search">
        <label>Search for more<Input value={query} disabled={disabled || batch.busy}
          onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); void runSearch() } }} /></label>
        <Button type="button" variant="outline" size="sm" disabled={disabled || batch.busy || searching || !query.trim()} onClick={() => void runSearch()}>{searching ? 'Searching…' : 'Search'}</Button>
        {results.filter(row => !selected.has(row.merchant_key)).map(row => <label key={row.merchant_key} className="merge-merchant-row">
          <input type="checkbox" checked={false} onChange={() => toggle(row.merchant_key)} />
          <span>{row.display_name ? `${row.display_name} (${row.merchant_key})` : row.merchant_key}</span>
          <small>{row.transaction_count} transaction{row.transaction_count === 1 ? '' : 's'}</small>
        </label>)}
      </div>
      <label>Display name to keep<Input value={displayName} disabled={disabled || batch.busy} maxLength={80}
        onChange={e => { setDisplayName(e.target.value); resetPreview() }} /></label>
      <label>Reason<Input value={reason} disabled={disabled || batch.busy} maxLength={2000}
        onChange={e => { setReason(e.target.value); resetPreview() }} /></label>
      {preview && <div className="merge-preview">
        <p className="muted-note">{preview.patterns.length} statement pattern{preview.patterns.length === 1 ? '' : 's'} combined · {preview.dependent_rules} rule{preview.dependent_rules === 1 ? '' : 's'} rewritten · {preview.affected_transactions} affected transaction{preview.affected_transactions === 1 ? '' : 's'}.</p>
        {preview.conflicts.map(conflict => <fieldset key={conflict.kind} className="merge-conflict" disabled={disabled || batch.busy}>
          <legend>Conflicting {conflict.kind}{conflict.kind === 'role' ? ' (spending pattern/need)' : ''} -- choose which to keep</legend>
          {conflict.candidates.map(candidate => <label key={candidate.from_merchant}>
            <input type="radio" name={`merge-conflict-${conflict.kind}`} checked={resolutions[conflict.kind] === candidate.from_merchant}
              onChange={() => { setResolutions(current => ({ ...current, [conflict.kind]: candidate.from_merchant })); resetPreview() }} />
            {' '}keep {JSON.stringify(candidate.value)} (from {candidate.from_merchant})
          </label>)}
        </fieldset>)}
        {unresolved.length > 0 && <p className="form-error">Resolve every conflict above, then preview again before saving.</p>}
      </div>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
      {preview && unresolved.length === 0 && <div className="preview" role="status"><Check size={15} /><p>Ready to merge {sources.length} merchant{sources.length === 1 ? '' : 's'} into {target}.</p></div>}
      {batch.submitted && batch.error && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
      <div className="dialog-actions">
        <Button type="button" variant="ghost" onClick={onClose} disabled={batch.busy}>Cancel</Button>
        <Button type="button" disabled={disabled || batch.busy || previewing || (preview ? unresolved.length > 0 : !canPreview)}
          onClick={() => { if (preview) void save(); else void runPreview() }}>
          {previewing ? 'Checking…' : batch.busy ? (batch.submitted && batch.error ? 'Retrying…' : 'Saving…') : preview ? 'Save merge' : 'Preview merge'}
        </Button>
      </div>
    </DialogContent>
  </Dialog>
}
