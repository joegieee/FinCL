import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { loadJourney } from '@/features/journey/api'

type PresetKey = 'month' | 'quarter' | 'ytd' | 'year'

const PRESETS: { key: PresetKey; label: string }[] = [
  { key: 'month', label: 'Latest month' },
  { key: 'quarter', label: 'Last 3 months' },
  { key: 'ytd', label: 'Year to date' },
  { key: 'year', label: 'Last 12 months' },
]

function isoDate(d: Date) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }

// Presets end on the last day the statements reach, not today, so a preset
// never asks for days the ledger cannot have yet.
function presetRange(preset: PresetKey, through: Date): { start: string; end: string } {
  const end = isoDate(through)
  if (preset === 'month') return { start: isoDate(new Date(through.getFullYear(), through.getMonth(), 1)), end }
  if (preset === 'quarter') { const start = new Date(through); start.setMonth(start.getMonth() - 3); return { start: isoDate(start), end } }
  if (preset === 'ytd') return { start: `${through.getFullYear()}-01-01`, end }
  const start = new Date(through); start.setFullYear(start.getFullYear() - 1); return { start: isoDate(start), end }
}

function formatLabel(value: string) {
  return new Date(value + 'T12:00:00').toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric' })
}

// The applied range is `start`/`end`; the popover holds its own draft so a
// keystroke in the date fields never looks applied until "Apply changes" is
// pressed. Presets apply immediately since they carry no ambiguity.
export function PeriodPicker({ start, end, applying, onApply }: {
  start: string; end: string; applying: boolean; onApply: (start: string, end: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [draftStart, setDraftStart] = useState(start)
  const [draftEnd, setDraftEnd] = useState(end)
  const [through, setThrough] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  useEffect(() => { loadJourney().then(page => setThrough(page.statements.complete_through)).catch(() => setThrough(null)) }, [])
  useEffect(() => { setDraftStart(start); setDraftEnd(end) }, [start, end])
  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) { if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false) }
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onClick); document.removeEventListener('keydown', onKey) }
  }, [open])
  const dirty = draftStart !== start || draftEnd !== end
  function choosePreset(key: PresetKey) {
    const range = presetRange(key, through ? new Date(`${through}T12:00:00`) : new Date())
    setOpen(false)
    onApply(range.start, range.end)
  }
  return <div className="period-picker" ref={containerRef}>
    <div className="period-presets" role="group" aria-label="Period presets">
      {PRESETS.map(p => <button type="button" key={p.key} onClick={() => choosePreset(p.key)}>{p.label}</button>)}
      <button type="button" className="period-custom-toggle" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)}>{formatLabel(start)} – {formatLabel(end)}</button>
    </div>
    {open && <div className="period-popover" role="dialog" aria-label="Custom period">
      <label>From<Input aria-label="From date" type="date" value={draftStart} onChange={e => setDraftStart(e.target.value)}/></label>
      <label>To<Input aria-label="To date" type="date" value={draftEnd} onChange={e => setDraftEnd(e.target.value)}/></label>
      <Button variant="outline" disabled={!dirty || applying || !draftStart || !draftEnd} onClick={() => { onApply(draftStart, draftEnd); setOpen(false) }}>Apply changes</Button>
    </div>}
  </div>
}
