import { useEffect, useState } from 'react'
import { Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useBatchCommand } from '@/lib/command'
import { ImpactDetails } from '@/features/rules'
import {
  compileRule, compileRoleRule, deriveRuleOptions,
  type CompiledRule, type CompiledRoleRule, type RoleRuleImpact, type RuleOption,
} from '@/features/rules/api'
import { label } from './api'

/**
 * Optional, explicitly disclosed reusable-rule creation for the shared
 * classification workflow. Reuses the same safe-predicate derivation
 * (`categorization/derive-rule`) and rule compile/impact services grouped
 * categorization's own "rule" scope already uses -- this is a second call
 * site for the identical backend capability, not a new domain implementation.
 *
 * Deliberately a *separate* atomic action from whatever the caller's own
 * selected-row edit saves: a rule here is derived from the frozen selection's
 * shared predicate, not the specific per-row values the caller may also be
 * about to save, so folding the two into one save would misrepresent them as
 * a single reviewed decision. When the user opts into both a category and a
 * role rule together, those two commands *are* committed atomically (one
 * `POST /api/v1/batch` call) -- but always as their own action, previewed and
 * saved distinctly from the row edit above it.
 */
export function RuleSection({ generation, anchors, category, costBehavior, need, reason, disabled, onSaved }: {
  generation: string
  /** The frozen selection a rule would be derived from -- exactly the same
   * anchors the caller's own row edit targets. */
  anchors: string[]
  /** The chosen field values from the caller's own draft that a rule would
   * apply going forward; '' means that field offers no rule (nothing to set). */
  category: string
  costBehavior: string
  need: string
  /** Reused verbatim from the caller's own form -- a rule never invents its
   * own reason separate from the one the user already gave for this draft. */
  reason: string
  disabled?: boolean
  onSaved: (status: string) => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [options, setOptions] = useState<RuleOption[] | null>(null)
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkError, setCheckError] = useState('')
  const [optionIndex, setOptionIndex] = useState<number | null>(null)
  const [wantCategoryRule, setWantCategoryRule] = useState(false)
  const [wantRoleRule, setWantRoleRule] = useState(false)
  const [compiling, setCompiling] = useState(false)
  const [compileError, setCompileError] = useState('')
  const [categoryCompiled, setCategoryCompiled] = useState<CompiledRule | null>(null)
  const [roleCompiled, setRoleCompiled] = useState<CompiledRoleRule | null>(null)

  const batch = useBatchCommand(generation, onSaved)

  const categoryEligible = Boolean(category)
  const roleEligible = Boolean(costBehavior || need)
  const anchorsKey = [...anchors].sort().join(',')

  function invalidateCompiled() { setCategoryCompiled(null); setRoleCompiled(null); setCompileError(''); batch.invalidate() }

  // Any change to what the rule would actually do -- the selection itself,
  // which field(s) it would set, the predicate chosen, or the reason --
  // invalidates a standing compile/preview, the same discipline every other
  // preview in this codebase applies to its own draft.
  useEffect(() => { setOptions(null); setUnavailableReason(null); setOptionIndex(null); invalidateCompiled()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchorsKey])
  useEffect(() => { invalidateCompiled()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [optionIndex, wantCategoryRule, wantRoleRule, category, costBehavior, need, reason])
  useEffect(() => { if (!categoryEligible) setWantCategoryRule(false) }, [categoryEligible])
  useEffect(() => { if (!roleEligible) setWantRoleRule(false) }, [roleEligible])

  async function checkPredicate() {
    setChecking(true); setCheckError('')
    try {
      const page = await deriveRuleOptions(generation, anchors)
      setOptions(page.options); setUnavailableReason(page.unavailable_reason)
      setOptionIndex(null)
    } catch (e) { setCheckError((e as Error).message) }
    finally { setChecking(false) }
  }

  const chosenMatch = optionIndex !== null ? options?.[optionIndex]?.match : undefined
  const canPreview = Boolean(chosenMatch) && (wantCategoryRule || wantRoleRule) && reason.trim() !== '' && !compiling

  async function previewRules() {
    if (!chosenMatch || !canPreview) return
    setCompiling(true); setCompileError('')
    try {
      const [categoryResult, roleResult] = await Promise.all([
        wantCategoryRule ? compileRule(chosenMatch, category, reason.trim()) : Promise.resolve(null),
        wantRoleRule ? compileRoleRule(generation, chosenMatch, costBehavior || null, need || null, reason.trim()) : Promise.resolve(null),
      ])
      setCategoryCompiled(categoryResult); setRoleCompiled(roleResult)
    } catch (e) { setCompileError((e as Error).message) }
    finally { setCompiling(false) }
  }

  async function saveRules() {
    const items = [categoryCompiled?.command, roleCompiled?.command].filter((c): c is NonNullable<typeof c> => Boolean(c))
    if (!items.length) return
    await batch.submit(items, reason.trim())
  }

  const compiled = Boolean(categoryCompiled || roleCompiled)
  const retrying = Boolean(batch.submitted && batch.error)

  if (!open) return <div className="rule-section-toggle">
    <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={() => setOpen(true)}>
      <Wand2 size={13}/>Also save a reusable rule from this selection
    </Button>
  </div>

  return <div className="rule-section">
    <div className="rule-section-head">
      <strong>Optional reusable rule</strong>
      <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>Hide</Button>
    </div>
    <p className="muted-note">
      A rule affects matching transactions in history and future imports, evaluated with the same
      precedence as everywhere else: a transaction's own manual override is never overwritten by a
      rule. This is a separate save from the edit above -- reviewing and saving it does not change
      or duplicate that edit.
    </p>
    {!categoryEligible && !roleEligible && <p className="muted-note">Choose a category, spending pattern, or spending need above to enable a reusable rule for it.</p>}
    {(categoryEligible || roleEligible) && <>
      {options === null && <Button type="button" variant="outline" size="sm" disabled={disabled || checking || !anchors.length} onClick={() => void checkPredicate()}>
        {checking ? 'Checking selection…' : 'Check for a reusable predicate'}
      </Button>}
      {checkError && <p className="form-error" role="alert">{checkError}</p>}
      {options !== null && unavailableReason && <p className="muted-note">{unavailableReason}.</p>}
      {options !== null && options.length > 0 && <fieldset className="rule-predicate" disabled={disabled}>
        <legend>Apply the rule to</legend>
        {options.map((option, index) => <label key={option.label}>
          <input type="radio" name="rule-predicate" checked={optionIndex === index} onChange={() => setOptionIndex(index)}/>
          {option.label}
        </label>)}
      </fieldset>}
      {options !== null && options.length > 0 && <div className="rule-kinds">
        {categoryEligible && <label>
          <input type="checkbox" checked={wantCategoryRule} disabled={disabled} onChange={e => setWantCategoryRule(e.target.checked)}/>
          {' '}Reusable category rule <small>({category})</small>
        </label>}
        {roleEligible && <label>
          <input type="checkbox" checked={wantRoleRule} disabled={disabled} onChange={e => setWantRoleRule(e.target.checked)}/>
          {' '}Reusable spending rule <small>({[costBehavior && label('cost_behavior', costBehavior), need && label('need', need)].filter(Boolean).join(' / ')})</small>
        </label>}
        {!reason.trim() && (wantCategoryRule || wantRoleRule) && <p className="muted-note">A reason is required above before creating a rule.</p>}
        {!compiled && <Button type="button" variant="outline" size="sm" disabled={!canPreview} onClick={() => void previewRules()}>
          {compiling ? 'Checking…' : 'Preview rule'}
        </Button>}
      </div>}
      {compileError && <p className="form-error" role="alert">{compileError}</p>}
      {categoryCompiled && <div className="rule-impact"><strong>Category rule impact</strong><ImpactDetails impact={categoryCompiled.impact}/></div>}
      {roleCompiled && <RoleImpactSummary impact={roleCompiled.impact}/>}
      {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
      {compiled && <Button type="button" disabled={disabled || batch.busy} onClick={() => void saveRules()}>
        {batch.busy ? 'Saving…' : retrying ? 'Retry same save' : 'Save rule'}
      </Button>}
      {retrying && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
    </>}
  </div>
}

function RoleImpactSummary({ impact }: { impact: RoleRuleImpact }) {
  return <div className="rule-impact" role="status">
    <strong>Spending-role rule impact</strong>
    <p>{impact.matches} matching transaction{impact.matches === 1 ? '' : 's'}; {impact.will_change} would change.
      {' '}{impact.protected_overrides
        ? `${impact.protected_overrides} manual override${impact.protected_overrides === 1 ? '' : 's'} ${impact.protected_overrides === 1 ? 'stays' : 'stay'} protected.`
        : 'No manual overrides are affected.'}</p>
  </div>
}
