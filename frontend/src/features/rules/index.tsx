import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, SlidersHorizontal, Tag } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { useLedgerCommand } from '@/lib/command'
import { compileRule, loadRules, PREDICATES, type CompiledRule, type RulesPage } from './api'

/**
 * v0.5 slice 3 — merchant aliases and category rules. Predicates are the
 * existing closed literal set plus an optional amount band; no regex rule
 * interpreter, no natural-language rules. The rule anchor hash is computed by
 * the server (/rules/compile), which also returns a conservative match count.
 */
export function RulesView({ generation, freshness, categories, onSaved }: {
  generation: string; freshness: string; categories: string[]
  onSaved: (status: string) => void | Promise<void>
}) {
  const [page, setPage] = useState<RulesPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadRules()
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  const afterSave = async (status: string) => { await onSaved(status); load() }
  const disabled = freshness === 'stale'

  if (loading && !page) return <div className="skeleton-page" aria-label="Loading rules"><div /><div /><div /></div>
  if (error && !page) {
    return <div className="alert error" role="alert"><div><strong>We couldn’t load rules.</strong><p>{error}</p></div><Button variant="outline" onClick={load}>Try again</Button></div>
  }
  if (!page) return null

  return (
    <div className="rules-view">
      <div className="scope-caption">
        <span>{page.rules.length} rule{page.rules.length === 1 ? '' : 's'} · {page.aliases.length} merchant alias{page.aliases.length === 1 ? '' : 'es'}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>
      {disabled && <div className="alert" role="status">New ledger inputs are available. Refresh before editing rules.</div>}

      <RuleBuilder generation={generation} categories={categories} disabled={disabled} onSaved={afterSave} />

      <Card className="rules-list">
        <div className="panel-title"><div><p className="eyebrow">CATEGORY RULES</p><h2>Applied newest first; a manual override still wins</h2></div></div>
        {page.rules.length === 0 && <div className="empty"><SlidersHorizontal size={22} /><h3>No rules yet</h3><p>Build one above. Rules assign a category to matching transactions unless you have overridden that transaction directly.</p></div>}
        {page.rules.map(rule => (
          <RuleRowView key={rule.rule_id} rule={rule} generation={generation} disabled={disabled} onSaved={afterSave} />
        ))}
      </Card>

      <AliasBuilder page={page} disabled={disabled} onSaved={afterSave} />
    </div>
  )
}

function RuleBuilder({ generation, categories, disabled, onSaved }: {
  generation: string; categories: string[]; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [predicate, setPredicate] = useState<typeof PREDICATES[number]['key']>('merchant_canonical')
  const [literal, setLiteral] = useState('')
  const [useBand, setUseBand] = useState(false)
  const [bandCurrency, setBandCurrency] = useState('SGD')
  const [bandMin, setBandMin] = useState('')
  const [bandMax, setBandMax] = useState('')
  const [category, setCategory] = useState(categories[0] ?? '')
  const [reason, setReason] = useState('')
  const [compiled, setCompiled] = useState<CompiledRule | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkError, setCheckError] = useState('')
  const cmd = useLedgerCommand(generation, async s => { clear(); await onSaved(s) })

  function clear() {
    setLiteral(''); setReason(''); setUseBand(false); setBandMin(''); setBandMax(''); setCompiled(null); setCheckError('')
  }
  function buildMatch(): Record<string, unknown> {
    const match: Record<string, unknown> = { [predicate]: literal.trim() }
    if (useBand) {
      const band: Record<string, unknown> = { currency: bandCurrency.trim().toUpperCase() }
      if (bandMin.trim()) band.min = bandMin.trim()
      if (bandMax.trim()) band.max = bandMax.trim()
      match.amount_band = band
    }
    return match
  }
  async function check() {
    setChecking(true); setCheckError(''); setCompiled(null); cmd.reset()
    try {
      setCompiled(await compileRule(buildMatch(), category, reason.trim()))
    } catch (e) {
      setCheckError((e as Error).message)
    } finally {
      setChecking(false)
    }
  }

  return (
    <Card className="rule-builder">
      <div className="panel-title"><div><p className="eyebrow">NEW RULE</p><h2>Assign a category to matching transactions</h2></div></div>
      <form onChange={() => { setCompiled(null); cmd.reset() }} onSubmit={e => { e.preventDefault(); if (compiled) void cmd.act(compiled.command); else void check() }}>
        <div className="form-row-3">
          <label>Match on
            <select value={predicate} disabled={cmd.attempted} onChange={e => setPredicate(e.target.value as typeof predicate)}>
              {PREDICATES.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
            </select>
          </label>
          <label>Value<Input required value={literal} disabled={cmd.attempted} onChange={e => setLiteral(e.target.value)} placeholder="e.g. CAFE SOCIAL" /></label>
          <label>Category
            <select required value={category} disabled={cmd.attempted} onChange={e => setCategory(e.target.value)}>
              {categories.filter(c => !c.startsWith('Internal')).map(c => <option key={c}>{c}</option>)}
            </select>
          </label>
        </div>
        <label className="inline-check"><input type="checkbox" checked={useBand} disabled={cmd.attempted} onChange={e => setUseBand(e.target.checked)} /> Also require an amount band</label>
        {useBand && (
          <div className="form-row-3">
            <label>Currency<Input value={bandCurrency} disabled={cmd.attempted} maxLength={3} onChange={e => setBandCurrency(e.target.value)} /></label>
            <label>Min<Input inputMode="decimal" value={bandMin} disabled={cmd.attempted} onChange={e => setBandMin(e.target.value)} placeholder="optional" /></label>
            <label>Max<Input inputMode="decimal" value={bandMax} disabled={cmd.attempted} onChange={e => setBandMax(e.target.value)} placeholder="optional" /></label>
          </div>
        )}
        <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} placeholder="A short note for your decision history" /></label>
        {checkError && <p className="form-error" role="alert">{checkError}</p>}
        {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
        {compiled && !cmd.previewed && (
          <div className="preview" role="status"><Check size={15} /><div><strong>Matches {compiled.match_count} recorded transaction{compiled.match_count === 1 ? '' : 's'}</strong><p>Existing manual overrides on those transactions are kept.</p></div></div>
        )}
        {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Ready to save this rule.</p></div>}
        {disabled && <p className="form-error">Refresh the ledger before adding a rule.</p>}
        <div className="dialog-actions">
          <Button type="submit" disabled={disabled || checking || cmd.busy || !literal.trim() || !reason.trim()}>
            {checking ? 'Checking…' : cmd.busy ? 'Working…' : !compiled ? 'Check match' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save rule') : 'Preview'}
          </Button>
        </div>
      </form>
    </Card>
  )
}

function RuleRowView({ rule, generation, disabled, onSaved }: {
  rule: RulesPage['rules'][number]; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const retire = useLedgerCommand(generation, onSaved)
  return (
    <div className="rule-row">
      <div>
        <code>{JSON.stringify(rule.match)}</code>
        <p>→ {rule.group_name} / {rule.category}{rule.reason ? ` · ${rule.reason}` : ''}</p>
      </div>
      <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
        onClick={() => void retire.act({ kind: 'CATEGORY_RULE_RETIRE', anchors: { rule_id: rule.rule_id }, payload: { reason: `Retire rule ${rule.rule_id}` } })}>
        {retire.previewed && !retire.attempted ? 'Confirm retire' : retire.busy ? 'Working…' : 'Retire'}
      </Button>
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
    </div>
  )
}

function AliasBuilder({ page, disabled, onSaved }: {
  page: RulesPage; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [canonical, setCanonical] = useState('')
  const [spec, setSpec] = useState('')
  const [display, setDisplay] = useState('')
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(page.generation, async s => { setCanonical(''); setSpec(''); setDisplay(''); setReason(''); await onSaved(s) })
  const retire = useLedgerCommand(page.generation, onSaved)

  function patterns() {
    return spec.split(/\s*,\s*/).filter(Boolean).map(part => {
      const [kind, ...rest] = part.split(':')
      return { [kind.trim()]: rest.join(':') }
    })
  }

  return (
    <Card className="alias-section">
      <div className="panel-title"><div><p className="eyebrow">MERCHANT ALIASES</p><h2>Map raw statement text to one canonical merchant</h2></div></div>
      {page.aliases.length > 0 && (
        <ul className="issue-list">
          {page.aliases.map(alias => (
            <li key={alias.canonical}>
              <Tag size={13} /> <strong>{alias.canonical}</strong>{alias.display_name ? ` (${alias.display_name})` : ''}
              <p>{alias.patterns.map(p => Object.entries(p).map(([k, v]) => `${k}:${v}`).join('')).join(', ')}</p>
              <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
                onClick={() => void retire.act({ kind: 'MERCHANT_ALIAS_RETIRE', anchors: { canonical: alias.canonical }, payload: { reason: `Retire alias ${alias.canonical}` } })}>Retire</Button>
            </li>
          ))}
        </ul>
      )}
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      <form onChange={cmd.reset} onSubmit={e => {
        e.preventDefault()
        void cmd.act({ kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: canonical.trim() },
          payload: display.trim() ? { patterns: patterns(), display_name: display.trim(), reason } : { patterns: patterns(), reason } })
      }}>
        <div className="form-row-3">
          <label>Canonical name<Input required value={canonical} disabled={cmd.attempted} onChange={e => setCanonical(e.target.value)} placeholder="CAFE SOCIAL" /></label>
          <label>Patterns<Input required value={spec} disabled={cmd.attempted} onChange={e => setSpec(e.target.value)} placeholder="exact:CAFE SOCIAL, prefix:CAFE" /><small>Comma-separated <code>exact:</code> / <code>prefix:</code> / <code>regex:</code>.</small></label>
          <label>Display name<Input value={display} disabled={cmd.attempted} maxLength={80} onChange={e => setDisplay(e.target.value)} placeholder="optional" /></label>
        </div>
        <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
        {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
        {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Normalize {spec} → {canonical.trim()}.</p></div>}
        {disabled && <p className="form-error">Refresh the ledger before adding an alias.</p>}
        <div className="dialog-actions">
          <Button type="submit" disabled={disabled || cmd.busy || !canonical.trim() || !spec.trim() || !reason.trim()}>
            {cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save alias') : 'Preview'}
          </Button>
        </div>
      </form>
    </Card>
  )
}
