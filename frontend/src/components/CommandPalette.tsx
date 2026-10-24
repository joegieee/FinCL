import { useEffect, useMemo, useState } from 'react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { commandDestinations, tabAliases, tabLabel } from '@/lib/tabs'

// Every destination in two keystrokes (Cmd/Ctrl+K, then Enter), so the rail
// never has to be tabbed through end to end.
export function CommandPalette({ open, onClose, onSelect }: {
  open: boolean; onClose: () => void; onSelect: (tab: string) => void
}) {
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  useEffect(() => { if (open) { setQuery(''); setHighlight(0) } }, [open])
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commandDestinations
    return commandDestinations.filter(d => tabLabel(d).toLowerCase().includes(q) || d.toLowerCase().includes(q))
  }, [query])
  useEffect(() => { setHighlight(h => Math.min(h, Math.max(0, matches.length - 1))) }, [matches.length])
  function choose(destination: string) { onSelect(tabAliases[destination] ?? destination); onClose() }
  return <Dialog open={open} onOpenChange={o => { if (!o) onClose() }}>
    <DialogContent className="command-palette" showCloseButton={false}>
      <DialogHeader><DialogTitle className="sr-only">Jump to a destination</DialogTitle><DialogDescription className="sr-only">Search and choose a destination by name</DialogDescription></DialogHeader>
      <Input
        autoFocus placeholder="Jump to…" aria-label="Search destinations" value={query}
        onChange={e => setQuery(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'ArrowDown') { e.preventDefault(); setHighlight(h => Math.min(matches.length - 1, h + 1)) }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setHighlight(h => Math.max(0, h - 1)) }
          else if (e.key === 'Enter' && matches[highlight]) { e.preventDefault(); choose(matches[highlight]) }
        }}
      />
      <ul className="command-list" role="listbox" aria-label="Destinations">
        {matches.map((d, i) => <li key={d}>
          <button type="button" role="option" aria-selected={i === highlight} className={i === highlight ? 'is-highlighted' : ''} onMouseEnter={() => setHighlight(i)} onClick={() => choose(d)}>
            {tabLabel(d)}{tabAliases[d] && <small> → {tabLabel(tabAliases[d])}</small>}
          </button>
        </li>)}
        {!matches.length && <li className="command-empty">No matching destination.</li>}
      </ul>
    </DialogContent>
  </Dialog>
}
