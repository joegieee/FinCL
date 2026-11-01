import { useEffect, useRef, useState } from 'react'
import { ChevronRight, History, Layers3, Moon, RefreshCw, Search, ShieldCheck, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/Tooltip'
import { railSections, sectionOfTab, tabLabel, tabMeta } from '@/lib/tabs'
import { nextTheme, readTheme, applyTheme, type Theme } from '@/lib/theme'

function sectionId(name: string) { return `rail-section-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}` }

function pendingCounts(reviewCount: number, proposalCount: number) {
  return (name: string) => (name === 'Transactions' ? reviewCount : name === 'Review' ? proposalCount : 0)
}

type RailActions = {
  ledgerLabel: string
  onSearch: () => void
  onHistory: () => void
  historyDisabled: boolean
  onRefresh: () => void
  refreshDisabled: boolean
  refreshing: boolean
  demo: boolean
}

/**
 * The main rail: six sections, with the current one open on its pages.
 *
 * The groups are the same six the tab table declares, so nothing is hidden and
 * nothing moved: the twenty-eight stay one click away, and only the section
 * you are in is on screen. Clicking a section opens its lead destination, which
 * is what opens it here, so a section row is never a dead end.
 *
 * Keyboard order walks the visible destinations in rail order.
 */
export function NavigationRail({ tab, onSelect, reviewCount, proposalCount, ...actions }: RailActions & {
  tab: string
  onSelect: (tab: string) => void
  reviewCount: number
  proposalCount: number
}) {
  const buttonRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const activeSection = sectionOfTab[tab]
  const countFor = pendingCounts(reviewCount, proposalCount)
  const visible = railSections.find(section => section.name === activeSection)?.tabs ?? []

  function focusAt(index: number) {
    const clamped = Math.max(0, Math.min(visible.length - 1, index))
    buttonRefs.current[visible[clamped]]?.focus()
  }
  function onKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === 'ArrowDown') { event.preventDefault(); focusAt(index + 1) }
    else if (event.key === 'ArrowUp') { event.preventDefault(); focusAt(index - 1) }
    else if (event.key === 'Home') { event.preventDefault(); focusAt(0) }
    else if (event.key === 'End') { event.preventDefault(); focusAt(visible.length - 1) }
  }

  return <nav className="nav-rail" aria-label="Main navigation" id="main-navigation">
    <Brand ledgerLabel={actions.ledgerLabel} onSelect={onSelect} />
    <button type="button" className="rail-search" onClick={actions.onSearch}><Search size={16} aria-hidden="true" />Go anywhere<kbd>{/Mac/.test(navigator.platform) ? '⌘ K' : 'Ctrl K'}</kbd></button>
    <div className="rail-sections">
      {railSections.map(section => {
        const headingId = sectionId(section.name)
        const open = section.name === activeSection
        const LeadIcon = tabMeta[section.lead]?.icon
        // A single-destination section is its own row: an Overview that expands
        // to reveal only Overview would be a step with nothing behind it.
        const solo = section.tabs.length === 1
        const pending = section.tabs.reduce((total, name) => total + countFor(name), 0)
        return <div className="rail-section" key={section.name}>
          <button
            className={`rail-group${open ? ' is-open' : ''}${solo && open ? ' is-current' : ''}`}
            aria-expanded={solo ? undefined : open}
            aria-current={solo && open ? 'page' : undefined}
            aria-controls={solo || !open ? undefined : headingId}
            onClick={() => onSelect(section.lead)}
          >
            {LeadIcon && <LeadIcon size={20} aria-hidden="true" />}
            <span className="rail-label">{section.label}</span>
            {!open && pending > 0 && <span className="rail-pending" aria-label={`${pending} waiting`} />}
            {!solo && <ChevronRight size={16} className="rail-chevron" aria-hidden="true" />}
          </button>
          {!solo && open && <ul id={headingId} aria-label={section.label}>
            {section.tabs.map((t, index) => {
              const count = countFor(t)
              return <li key={t}><button
                ref={el => { buttonRefs.current[t] = el }}
                aria-current={tab === t ? 'page' : undefined}
                tabIndex={tab === t ? 0 : -1}
                onClick={() => onSelect(t)}
                onKeyDown={e => onKeyDown(e, index)}
              >
                <span className="rail-label">{tabLabel(t)}</span>
                {count > 0 && <span className="nav-count">{count}</span>}
              </button></li>
            })}
          </ul>}
        </div>
      })}
    </div>
    {actions.demo && <p className="rail-note">Synthetic demo</p>}
    <div className="rail-actions"><RailTools {...actions} /></div>
  </nav>
}

/** History, theme and refresh, plus search on the top bar, which has no search row. */
function RailTools({ onSearch, onHistory, historyDisabled, onRefresh, refreshDisabled, refreshing, compact = false }: RailActions & { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>(() => readTheme())
  useEffect(() => { applyTheme(theme) }, [theme])
  return <>
    {compact && <Tooltip label="Go anywhere"><Button variant="ghost" size="icon-sm" aria-label="Go anywhere" onClick={onSearch}><Search size={16} /></Button></Tooltip>}
    <Tooltip label="History"><Button variant="ghost" size="icon-sm" aria-label="History" disabled={historyDisabled} onClick={onHistory}><History size={16} /></Button></Tooltip>
    <Tooltip label={themeLabel(theme)}><Button variant="ghost" size="icon-sm" aria-label={themeLabel(theme)} onClick={() => setTheme(nextTheme(theme))}>{theme === 'dark' ? <Moon size={16} /> : <Sun size={16} />}</Button></Tooltip>
    <Tooltip label="Refresh"><Button variant="ghost" size="icon-sm" aria-label="Refresh" disabled={refreshDisabled} onClick={onRefresh}><RefreshCw size={16} className={refreshing ? 'spin' : ''} /></Button></Tooltip>
    {!compact && <Tooltip label="Local and private"><span className="rail-local" tabIndex={0}><ShieldCheck size={16} aria-label="Local and private" /></span></Tooltip>}
  </>
}

function Brand({ ledgerLabel, onSelect }: { ledgerLabel: string; onSelect: (tab: string) => void }) {
  return <a className="rail-brand" href="?view=Overview" onClick={e => { e.preventDefault(); onSelect('Overview') }}>
    <span className="brand-icon"><Layers3 size={20} /></span>
    <span className="rail-brand-label"><span className="rail-brand-name">fincl</span><span className="ledger-name">{ledgerLabel}</span></span>
  </a>
}

/** Small screens have no sidebar, so what lives at its top and foot moves here. */
export function TopBar({ onSelect, ...actions }: RailActions & { onSelect: (tab: string) => void }) {
  return <header className="top-bar">
    <Brand ledgerLabel={actions.ledgerLabel} onSelect={onSelect} />
    <div className="top-bar-tools"><RailTools {...actions} compact /></div>
  </header>
}

/** The six sections, along the foot of a small screen. */
export function SectionBar({ tab, onSelect, reviewCount, proposalCount }: {
  tab: string
  onSelect: (tab: string) => void
  reviewCount: number
  proposalCount: number
}) {
  const activeSection = sectionOfTab[tab]
  const countFor = pendingCounts(reviewCount, proposalCount)
  return <nav className="section-bar" aria-label="Sections">
    {railSections.map(section => {
      const LeadIcon = tabMeta[section.lead]?.icon
      const pending = section.tabs.reduce((total, name) => total + countFor(name), 0)
      return <button key={section.name} aria-current={section.name === activeSection ? 'true' : undefined} onClick={() => onSelect(section.lead)}>
        {LeadIcon && <LeadIcon size={20} aria-hidden="true" />}
        <span className="rail-label">{section.label}</span>
        {pending > 0 && <span className="rail-pending" aria-label={`${pending} waiting`} />}
      </button>
    })}
  </nav>
}

/** The current section's pages, under the title, where the sidebar would list them. */
export function SectionTabs({ tab, onSelect, reviewCount, proposalCount }: {
  tab: string
  onSelect: (tab: string) => void
  reviewCount: number
  proposalCount: number
}) {
  const section = railSections.find(item => item.name === sectionOfTab[tab])
  const countFor = pendingCounts(reviewCount, proposalCount)
  if (!section || section.tabs.length < 2) return null
  return <nav className="section-tabs" aria-label={`${section.label} pages`}>
    {section.tabs.map(t => <button key={t} aria-current={tab === t ? 'page' : undefined} onClick={() => onSelect(t)}>
      {tabLabel(t)}{countFor(t) > 0 && <span className="nav-count">{countFor(t)}</span>}
    </button>)}
  </nav>
}

function themeLabel(theme: Theme) {
  return theme === 'system' ? 'Theme: system' : theme === 'dark' ? 'Theme: dark' : 'Theme: light'
}
