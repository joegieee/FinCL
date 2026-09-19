import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

// The one recent-category list, shared by every picker in the app: grouped
// review used to keep a second one behind its own combobox, which meant the
// two disagreed about what you had just used (and the inbox's copy mislabelled
// it "Suggested" -- docs/PLAN-unified-transaction-classification.md Step 0).
const RECENT_KEY = 'fincl.classification.recentCategories'

export function loadRecentCategories(): string[] {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]') } catch { return [] }
}
export function rememberCategory(category: string) {
  try {
    const next = [category, ...loadRecentCategories().filter(c => c !== category)].slice(0, 6)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch { /* private browsing or storage disabled */ }
}

type CustomCategoryMeta = { key: string; archived: boolean }

/** Excludes `Internal /` categories and archived custom categories (Step 0
 * finding: existing pickers only filtered `Internal /`, silently offering an
 * archived custom category for new assignment). */
export function useAssignableCategories(categories: string[]): string[] {
  const [archived, setArchived] = useState<Set<string>>(new Set())
  useEffect(() => {
    let live = true
    api<{ categories: CustomCategoryMeta[] }>('categories')
      .then(page => { if (live) setArchived(new Set(page.categories.filter(c => c.archived).map(c => c.key))) })
      .catch(() => { /* best-effort metadata; an unfiltered list is still usable */ })
    return () => { live = false }
  }, [])
  return categories.filter(c => !c.startsWith('Internal /') && !archived.has(c))
}

export function CategoryPicker({ id, categories, value, onChange, onCreate, disabled, label = 'Category' }: {
  id: string
  categories: string[]
  value: string
  onChange: (category: string) => void
  /** Supplied by callers that allow inline creation of a missing category
   * (grouped review): returns whether the category now exists. Without it the
   * picker only offers what already exists. */
  onCreate?: (name: string) => Promise<boolean>
  disabled?: boolean
  label?: string
}) {
  const options = useAssignableCategories(categories)
  const recent = loadRecentCategories().filter(c => options.includes(c))
  const ordered = [...new Set([...recent, ...options])]
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [creating, setCreating] = useState(false)
  useEffect(() => { setQuery(value) }, [value])
  const filtered = query.trim() ? ordered.filter(c => c.toLowerCase().includes(query.trim().toLowerCase())) : ordered
  function choose(category: string) { onChange(category); setQuery(category); setOpen(false) }
  /** A new category is always "Group / Name" (`custom_categories.py`
   * `validate_creation`), so a bare word is not offered for creation and
   * pressing Enter on one does nothing rather than posting a request the
   * ledger is certain to reject. */
  const trimmed = query.trim()
  const creatable = Boolean(onCreate) && trimmed !== '' && !options.includes(trimmed) && trimmed.includes(' / ')
  async function createFromQuery() {
    if (!onCreate || creating || !creatable) return
    setCreating(true)
    try { if (await onCreate(trimmed)) choose(trimmed) }
    finally { setCreating(false) }
  }
  return <div className="category-combobox">
    <label htmlFor={id}>{label}</label>
    <input
      id={id} role="combobox" aria-expanded={open} aria-autocomplete="list" aria-controls={`${id}-list`} autoComplete="off"
      disabled={disabled} value={query} placeholder={onCreate ? 'Search or create a category…' : 'Search a category…'}
      onChange={event => { setQuery(event.target.value); setOpen(true); setHighlight(0) }}
      onFocus={() => setOpen(true)}
      onBlur={() => setTimeout(() => setOpen(false), 150)}
      onKeyDown={event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setHighlight(h => Math.min(h + 1, Math.max(filtered.length - 1, 0))) }
        else if (event.key === 'ArrowUp') { event.preventDefault(); setHighlight(h => Math.max(h - 1, 0)) }
        else if (event.key === 'Enter') {
          if (open && filtered[highlight]) { event.preventDefault(); event.stopPropagation(); choose(filtered[highlight]) }
          else if (creatable) { event.preventDefault(); event.stopPropagation(); void createFromQuery() }
        }
        else if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false) }
      }}
    />
    {open && <ul className="category-combobox-list" id={`${id}-list`} role="listbox">
      {filtered.map((category, i) => <li key={category} role="option" aria-selected={category === value}>
        <button type="button" className={i === highlight ? 'is-active' : ''} onMouseDown={event => event.preventDefault()} onClick={() => choose(category)}>
          <span>{category}</span>{recent.includes(category) && <span className="tag">Recent</span>}
        </button>
      </li>)}
      {!filtered.length && <li className="category-combobox-empty">No category matches "{query}".</li>}
      {creatable && <li><button type="button" onMouseDown={event => event.preventDefault()} onClick={() => void createFromQuery()} disabled={creating}>
        {creating ? 'Creating…' : `Create "${trimmed}"`}
      </button></li>}
    </ul>}
    {onCreate && trimmed !== '' && !options.includes(trimmed) && !trimmed.includes(' / ') && <small>Type "Group / Name" to create a new category inline.</small>}
  </div>
}
