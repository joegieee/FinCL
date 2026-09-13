import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, SlidersHorizontal, Tag } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { useLedgerCommand } from '@/lib/command'
import { api } from '@/lib/api'
import { compileRule, loadRoleRules, loadRules, PREDICATES, type CompiledRule, type RoleRuleRow, type RulesPage } from './api'

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

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={load} loadingLabel="Loading rules" errorTitle="We couldn’t load rules.">
      {page => <RulesContent page={page} generation={generation} categories={categories} disabled={disabled} loading={loading} load={load} afterSave={afterSave} />}
    </AsyncView>
  )
}

function RulesContent({ page, generation, categories, disabled, loading, load, afterSave }: {
  page: RulesPage; generation: string; categories: string[]; disabled: boolean; loading: boolean; load: () => void; afterSave: (status: string) => Promise<void>
}) {
  const inactiveRules = page.inactive_rules ?? []

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

      {inactiveRules.length > 0 && (
        <Card className="rules-list inactive-rules">
          <div className="panel-title"><div><p className="eyebrow">INACTIVE RULES</p><h2>{inactiveRules.length} retired rule{inactiveRules.length === 1 ? '' : 's'}</h2></div></div>
          {inactiveRules.map(rule => <InactiveRuleRow key={rule.rule_id} rule={rule} generation={generation} disabled={disabled} onSaved={afterSave} />)}
        </Card>
      )}

      <RoleRulesPanel generation={generation} disabled={disabled} onSaved={afterSave} />

      <AliasBuilder page={page} disabled={disabled} onSaved={afterSave} />
    </div>
  )
}

function RoleRulesPanel({ generation, disabled, onSaved }: {
  generation: string; disabled: boolean; onSaved: (status: string) => Promise<void>
}) {
  const [rules, setRules] = useState<RoleRuleRow[] | null>(null)
  const [error, setError] = useState('')
  const seq = useRef(0)
  const load = useCallback(() => {
    const id = ++seq.current
    setError('')
    loadRoleRules()
      .then(p => { if (id === seq.current) setRules(p.rules) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
  }, [])
  useEffect(() => { load() }, [load])
  const afterSave = async (status: string) => { await onSaved(status); load() }

  return (
    <Card className="rules-list">
      <div className="panel-title"><div><p className="eyebrow">COST-BEHAVIOR & NEED RULES</p><h2>Reusable role rules; a manual entry override still wins</h2></div></div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {rules?.length === 0 && <div className="empty"><SlidersHorizontal size={22} /><h3>No role rules yet</h3><p>Create one from Transactions or Categorization: select rows, set a behavior or need, then “Also save a reusable rule from this selection”. Reactivating a retired rule is the same flow.</p></div>}
      {(rules ?? []).map(rule => <RoleRuleRow key={rule.rule_id} rule={rule} generation={generation} disabled={disabled} onSaved={afterSave} />)}
    </Card>
  )
}

function RoleRuleRow({ rule, generation, disabled, onSaved }: {
  rule: RoleRuleRow; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const retire = useLedgerCommand(generation, onSaved)
  const dimensions = [rule.cost_behavior && `behavior → ${rule.cost_behavior.replaceAll('_', ' ')}`, rule.need && `need → ${rule.need}`].filter(Boolean).join(' · ')
  return (
    <div className="rule-row">
      <div>
        <strong>{ruleSentence(rule.match)}</strong>
        <p>{dimensions || 'no dimension set'}</p>
        {rule.reason && <small>{rule.reason}</small>}
        {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      </div>
      <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
        onClick={() => void retire.act({ kind: 'ROLE_RULE_RETIRE', anchors: { rule_id: rule.rule_id }, payload: { reason: `Retire role rule ${rule.rule_id}` } })}>
        {retire.busy ? 'Working…' : retire.previewed ? 'Confirm retire' : 'Retire'}
      </Button>
    </div>
  )
}

function InactiveRuleRow({ rule, generation, disabled, onSaved }: {
  rule: RulesPage['inactive_rules'][number]; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [compiled, setCompiled] = useState<CompiledRule | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState('')
  const command = useLedgerCommand(generation, onSaved)
  const category = `${rule.group_name} / ${rule.category}`

  async function checkReactivation() {
    setChecking(true); setError(''); setCompiled(null); command.reset()
    try {
      setCompiled(await compileRule(rule.match, category, reason.trim()))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="rule-row inactive-rule-row">
      <div>
        <strong>{ruleSentence(rule.match)} → {category}</strong>
        <p>Not active. Restore the same predicate and category with a new journal reason.</p>
        <label>Reason<Input required maxLength={2000} value={reason} disabled={command.attempted} onChange={e => { setReason(e.target.value); setCompiled(null); command.reset() }} placeholder="Why restore this rule?" /></label>
        {compiled && !command.previewed && <p className="muted-note">Reactivation will assign {compiled.impact.effective_assignments} matching transaction{compiled.impact.effective_assignments === 1 ? '' : 's'}; {compiled.impact.newly_categorized + compiled.impact.recategorized} will change.</p>}
        {compiled && command.previewed && <p className="muted-note">Ready to restore this rule.</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {command.error && <p className="form-error" role="alert">{command.error}</p>}
      </div>
      <Button variant="ghost" size="sm" disabled={disabled || checking || command.busy || !reason.trim()}
        onClick={() => { if (compiled) void command.act(compiled.command); else void checkReactivation() }}>
        {checking ? 'Checking…' : command.busy ? 'Working…' : !compiled ? 'Check reactivation impact' : command.previewed ? (command.attempted ? 'Retry same save' : 'Restore rule') : 'Preview restore'}
      </Button>
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
  // Choosing a category is a durable decision.  Never silently use the first
  // taxonomy item merely because the picker has loaded.
  const [category, setCategory] = useState('')
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
              <option value="" disabled>Choose a category</option>
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
        {compiled && !cmd.previewed && <ImpactDetails impact={compiled.impact} />}
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

export function ImpactDetails({ impact }: { impact: CompiledRule['impact'] }) {
  const changed = impact.newly_categorized + impact.recategorized
  const currencies = impact.breakdowns?.matched.by_currency ?? {}
  return <div className="preview impact-preview" role="status"><Check size={15} /><div><strong>{changed} transaction{changed === 1 ? '' : 's'} will change</strong><p>{impact.predicate_matches} match the predicate; {impact.effective_assignments} will be assigned by this rule. {impact.protected_by_override ? `${impact.protected_by_override} manual override${impact.protected_by_override === 1 ? '' : 's'} stay protected. ` : ''}{impact.inactive ? `${impact.inactive} inactive match${impact.inactive === 1 ? '' : 'es'} are included in the preview. ` : ''}{impact.direction_conflict ? `${impact.direction_conflict} direction conflict${impact.direction_conflict === 1 ? '' : 's'} need attention.` : 'No direction conflicts.'}</p>{Object.keys(currencies).length > 0 && <small>{Object.entries(currencies).map(([currency, value]) => `${value.count} ${currency} (${value.absolute_amount})`).join(' · ')}</small>}{impact.overlaps?.map(overlap => <p className="muted-note" key={overlap.rule_id}>Overlap with {overlap.rule_id}: {overlap.observed_matches} observed match{overlap.observed_matches === 1 ? '' : 'es'} — {overlap.message}</p>)}{impact.warnings?.map(warning => <p className="form-error" key={warning}>{warning}</p>)}</div></div>
}

function RuleRowView({ rule, generation, disabled, onSaved }: {
  rule: RulesPage['rules'][number]; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const retire = useLedgerCommand(generation, onSaved)
  const [history, setHistory] = useState<Array<{ journal_seq: number; kind: string; recorded_at: string; reason: string }> | null>(null)
  const [historyError, setHistoryError] = useState('')
  const [retirementImpact, setRetirementImpact] = useState<{ effective_assignments_removed: number; newly_unclassified: number; recategorized: number } | null>(null)
  async function loadHistory() {
    try { setHistoryError(''); setHistory((await api<{ history: Array<{ journal_seq: number; kind: string; recorded_at: string; reason: string }> }>(`rules/${encodeURIComponent(rule.rule_id)}/history`)).history) }
    catch (e) { setHistoryError((e as Error).message) }
  }
  async function retireWithImpact() {
    if (!retirementImpact) {
      try { setRetirementImpact((await api<{ impact: { effective_assignments_removed: number; newly_unclassified: number; recategorized: number } }>(`rules/${encodeURIComponent(rule.rule_id)}/retirement-impact?generation=${encodeURIComponent(generation)}`)).impact) }
      catch (e) { setHistoryError((e as Error).message) }
      return
    }
    await retire.act({ kind: 'CATEGORY_RULE_RETIRE', anchors: { rule_id: rule.rule_id }, payload: { reason: `Retire rule ${rule.rule_id}` } })
  }
  return (
    <div className="rule-row">
      <div>
        <strong>{ruleSentence(rule.match)} → {rule.group_name} / {rule.category}</strong>
        <p>{rule.metrics.effective_assignments} effective assignment{rule.metrics.effective_assignments === 1 ? '' : 's'} from {rule.metrics.predicate_matches} observed match{rule.metrics.predicate_matches === 1 ? '' : 'es'}{rule.metrics.overridden ? ` · ${rule.metrics.overridden} manual override${rule.metrics.overridden === 1 ? '' : 's'}` : ''}{rule.metrics.direction_conflicts ? ` · ${rule.metrics.direction_conflicts} direction conflict${rule.metrics.direction_conflicts === 1 ? '' : 's'}` : ''}</p>
        <small>{rule.metrics.first_match ? `Observed ${rule.metrics.first_match} to ${rule.metrics.last_match}` : 'No current observed matches'}{rule.reason ? ` · ${rule.reason}` : ''}</small>
      </div>
      <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
        onClick={() => void retireWithImpact()}>
        {!retirementImpact ? 'Check deactivation impact' : retire.previewed && !retire.attempted ? 'Confirm retire' : retire.busy ? 'Working…' : 'Preview retire'}
      </Button>
      <Button variant="ghost" size="sm" onClick={() => void loadHistory()}>History</Button>
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      {historyError && <p className="form-error" role="alert">{historyError}</p>}
      {retirementImpact && <p className="muted-note">Deactivation removes {retirementImpact.effective_assignments_removed} effective assignment{retirementImpact.effective_assignments_removed === 1 ? '' : 's'}; {retirementImpact.newly_unclassified} become unclassified and {retirementImpact.recategorized} fall through to another category.</p>}
      {history && <ol className="rule-history">{history.map(item => <li key={`${item.journal_seq}-${item.kind}`}><strong>{item.kind.replaceAll('_', ' ').toLowerCase()}</strong> · {item.recorded_at}{item.reason ? ` · ${item.reason}` : ''}</li>)}</ol>}
    </div>
  )
}

function ruleSentence(match: Record<string, unknown>) {
  if ('merchant_canonical' in match) return `Merchant is “${match.merchant_canonical}”`
  if ('paynow_to' in match) return `PayNow recipient is “${match.paynow_to}”`
  if ('description_prefix' in match) return `Description starts with “${match.description_prefix}”`
  return `Description contains “${match.description_contains}”${match.account ? ` in ${match.account}` : ''}`
}

function AliasBuilder({ page, disabled, onSaved }: {
  page: RulesPage; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [canonical, setCanonical] = useState('')
  const [spec, setSpec] = useState('')
  const [display, setDisplay] = useState('')
  const [reason, setReason] = useState('')
  const [history, setHistory] = useState<{ canonical: string; items: Array<{ journal_seq: number; kind: string; recorded_at: string; reason: string }> } | null>(null)
  const [historyError, setHistoryError] = useState('')
  const cmd = useLedgerCommand(page.generation, async s => { setCanonical(''); setSpec(''); setDisplay(''); setReason(''); await onSaved(s) })
  const retire = useLedgerCommand(page.generation, onSaved)

  function patterns() {
    return spec.split(/\s*,\s*/).filter(Boolean).map(part => {
      const [kind, ...rest] = part.split(':')
      return { [kind.trim()]: rest.join(':') }
    })
  }

  async function loadHistory(canonical: string) {
    try {
      setHistoryError('')
      const response = await api<{ history: Array<{ journal_seq: number; kind: string; recorded_at: string; reason: string }> }>(`aliases/${encodeURIComponent(canonical)}/history`)
      setHistory({ canonical, items: response.history })
    } catch (e) {
      setHistoryError((e as Error).message)
    }
  }

  return (
    <Card className="alias-section">
      <div className="panel-title"><div><p className="eyebrow">MERCHANT ALIASES</p><h2>Map raw statement text to one canonical merchant</h2></div></div>
      {page.aliases.length > 0 && (
        <ul className="issue-list">
          {page.aliases.map(alias => (
            <li key={alias.canonical}>
              <Tag size={13} /> <strong>{alias.canonical}</strong>{alias.display_name ? ` (${alias.display_name})` : ''}
              <p>{alias.metrics.observed_matches} observed match{alias.metrics.observed_matches === 1 ? '' : 'es'} · {alias.metrics.effective_assignments} effective assignment{alias.metrics.effective_assignments === 1 ? '' : 's'}</p>
              <small>{alias.patterns.map(p => Object.entries(p).map(([k, v]) => `${k}: ${v}`).join('')).join(', ')}{alias.metrics.first_match ? ` · ${alias.metrics.first_match} to ${alias.metrics.last_match}` : ''}</small>
              <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
                onClick={() => void retire.act({ kind: 'MERCHANT_ALIAS_RETIRE', anchors: { canonical: alias.canonical }, payload: { reason: `Retire alias ${alias.canonical}` } })}>Retire</Button>
              <Button variant="ghost" size="sm" onClick={() => void loadHistory(alias.canonical)}>History</Button>
              {history?.canonical === alias.canonical && <ol className="rule-history">{history.items.map(item => <li key={`${item.journal_seq}-${item.kind}`}><strong>{item.kind.replaceAll('_', ' ').toLowerCase()}</strong> · {item.recorded_at}{item.reason ? ` · ${item.reason}` : ''}</li>)}</ol>}
            </li>
          ))}
        </ul>
      )}
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      {historyError && <p className="form-error" role="alert">{historyError}</p>}
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
