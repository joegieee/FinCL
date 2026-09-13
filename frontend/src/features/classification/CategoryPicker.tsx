import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

// Deliberately separate from CategorizationView's own recent-category storage
// (docs/PLAN-unified-transaction-classification.md Step 0 finding: that
// picker mislabels this list "Suggested"; renaming it there is Step 2/6
// follow-up work, left untouched here). This picker gets the corrected
// "Recent" label from the start.
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

export function CategoryPicker({ id, categories, value, onChange, disabled }: {
  id: string
  categories: string[]
  value: string
  onChange: (category: string) => void
  disabled?: boolean
}) {
  const options = useAssignableCategories(categories)
  const recent = loadRecentCategories().filter(c => options.includes(c))
  const ordered = [...new Set([...recent, ...options])]
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  useEffect(() => { setQuery(value) }, [value])
  const filtered = query.trim() ? ordered.filter(c => c.toLowerCase().includes(query.trim().toLowerCase())) : ordered
  function choose(category: string) { onChange(category); setQuery(category); setOpen(false) }
  return <div className="category-combobox">
    <label htmlFor={id}>Category</label>
    <input
      id={id} role="combobox" aria-expanded={open} aria-autocomplete="list" aria-controls={`${id}-list`} autoComplete="off"
      disabled={disabled} value={query} placeholder="Search a category…"
      onChange={event => { setQuery(event.target.value); setOpen(true); setHighlight(0) }}
      onFocus={() => setOpen(true)}
      onBlur={() => setTimeout(() => setOpen(false), 150)}
      onKeyDown={event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setHighlight(h => Math.min(h + 1, Math.max(filtered.length - 1, 0))) }
        else if (event.key === 'ArrowUp') { event.preventDefault(); setHighlight(h => Math.max(h - 1, 0)) }
        else if (event.key === 'Enter') { if (open && filtered[highlight]) { event.preventDefault(); choose(filtered[highlight]) } }
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
    </ul>}
  </div>
}
