import { useEffect, useRef, useState } from 'react'
import { ChevronRight, ChevronsLeft, ChevronsRight, History, Layers3, Moon, RefreshCw, ShieldCheck, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tooltip } from '@/components/Tooltip'
import { railSections, sectionOfTab, tabLabel, tabMeta, type RailSection, type RailSectionName } from '@/lib/tabs'
import { nextTheme, readTheme, applyTheme, type Theme } from '@/lib/theme'

function sectionId(name: string) { return `rail-section-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}` }

/**
 * The main rail: six groups, each expanding to its own destinations.
 *
 * Twenty-eight equal rows made finding anything a scan of the whole list. The
 * groups are the same six the tab table already declared — nothing is hidden and
 * nothing moved to a different section — but only the active group's children
 * are on screen, so the rail shows six things and the twenty-eight stay one
 * click away. Clicking a group opens its lead destination rather than only
 * toggling it, so a group row is never a dead end.
 *
 * Keyboard order still walks every visible destination in rail order, and a
 * collapsed rail keeps each group reachable through its tooltip and lead tab.
 */
export function NavigationRail({
  tab, onSelect, reviewCount, proposalCount, collapsed, onToggleCollapse, ledgerLabel, drawer = false,
  onHistory, historyDisabled, onRefresh, refreshDisabled, refreshing, demo,
}: {
  tab: string
  onSelect: (tab: string) => void
  reviewCount: number
  proposalCount: number
  collapsed: boolean
  onToggleCollapse: () => void
  ledgerLabel: string
  drawer?: boolean
  onHistory: () => void
  historyDisabled: boolean
  onRefresh: () => void
  refreshDisabled: boolean
  refreshing: boolean
  demo: boolean
}) {
  const buttonRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const activeSection = sectionOfTab[tab]
  // Which groups are open. The active group is always open; this holds the
  // ones the operator opened on top of it, so browsing a second group does not
  // close the one they came from.
  const [opened, setOpened] = useState<RailSectionName[]>([])
  const [theme, setTheme] = useState<Theme>(() => readTheme())
  useEffect(() => { applyTheme(theme) }, [theme])

  const isOpen = (section: RailSection) =>
    section.name === activeSection || opened.includes(section.name)

  // Only what is actually on screen takes part in arrow-key movement, so the
  // keys never jump focus into a collapsed group.
  const visible = railSections.flatMap(section => (isOpen(section) ? section.tabs : []))

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

  function openGroup(section: RailSection) {
    setOpened(current =>
      current.includes(section.name)
        ? current.filter(name => name !== section.name)
        : [...current, section.name],
    )
    // In the mobile drawer a group only expands. Navigating closes the drawer,
    // which would put the destinations out of reach the instant they appeared;
    // on a phone the group row is a way in, not a shortcut past it.
    if (!drawer) onSelect(section.lead)
  }

  function countFor(name: string) {
    return name === 'Transactions' ? reviewCount : name === 'Review' ? proposalCount : 0
  }

  return <nav className={`nav-rail${collapsed && !drawer ? ' is-collapsed' : ''}`} aria-label="Main navigation" id="main-navigation">
    <a className="rail-brand" href="?view=Overview" onClick={e => { e.preventDefault(); onSelect('Overview') }}>
      <span className="brand-icon"><Layers3 size={19}/></span>
      <span className="rail-brand-label"><span className="rail-brand-name">fincl</span><span className="ledger-name">{ledgerLabel}</span></span>
    </a>
    <div className="rail-sections">
      {railSections.map(section => {
        const headingId = sectionId(section.name)
        const open = isOpen(section)
        const LeadIcon = tabMeta[section.lead]?.icon
        // A single-destination group is its own row: an Overview that expands
        // to reveal only Overview would be a step with nothing behind it.
        const solo = section.tabs.length === 1
        const pending = section.tabs.reduce((total, name) => total + countFor(name), 0)
        const groupButton = <button
          className={`rail-group${open ? ' is-open' : ''}${solo && tab === section.lead ? ' is-solo-active' : ''}`}
          aria-expanded={solo ? undefined : open}
          aria-current={solo && tab === section.lead ? 'page' : undefined}
          aria-controls={solo ? undefined : headingId}
          onClick={() => (solo ? onSelect(section.lead) : openGroup(section))}
        >
          {LeadIcon && <LeadIcon size={17}/>}
          <span className="rail-label">{section.label}</span>
          {!open && pending > 0 && <span className="nav-count">{pending}</span>}
          {!solo && <ChevronRight size={14} className="rail-chevron"/>}
        </button>
        return <div className="rail-section" key={section.name}>
          {collapsed && !drawer ? <Tooltip label={section.label}>{groupButton}</Tooltip> : groupButton}
          {!solo && open && <ul id={headingId} aria-label={section.label}>
            {section.tabs.map(t => {
              const meta = tabMeta[t]
              const Icon = meta.icon
              const active = tab === t
              const index = visible.indexOf(t)
              const count = countFor(t)
              const button = <button
                ref={el => { buttonRefs.current[t] = el }}
                aria-current={active ? 'page' : undefined}
                tabIndex={active || (!visible.includes(tab) && index === 0) ? 0 : -1}
                onClick={() => onSelect(t)}
                onKeyDown={e => onKeyDown(e, index)}
              >
                {Icon && <Icon size={15}/>}
                <span className="rail-label">{tabLabel(t)}</span>
                {count > 0 && <span className="nav-count">{count}</span>}
              </button>
              return <li key={t}>{collapsed && !drawer ? <Tooltip label={tabLabel(t)}>{button}</Tooltip> : button}</li>
            })}
          </ul>}
        </div>
      })}
    </div>
    <div className="rail-actions">
      {maybeTooltip(collapsed && !drawer, 'History', <Button variant="ghost" size="sm" disabled={historyDisabled} onClick={onHistory}><History size={16}/><span className="rail-label">History</span></Button>)}
      {maybeTooltip(collapsed && !drawer, themeLabel(theme), <Button variant="ghost" size="sm" aria-label={themeLabel(theme)} onClick={() => setTheme(nextTheme(theme))}>{theme === 'dark' ? <Moon size={16}/> : <Sun size={16}/>}<span className="rail-label">{themeLabel(theme)}</span></Button>)}
      {maybeTooltip(collapsed && !drawer, 'Local & private', <span className="local"><ShieldCheck size={14}/><span className="rail-label">Local & private</span></span>)}
      {demo && (collapsed && !drawer ? <Tooltip label="Synthetic demo"><Badge variant="outline" className="rail-demo-badge">D</Badge></Tooltip> : <Badge variant="outline">Synthetic demo</Badge>)}
      <div className="rail-actions-row">
        {maybeTooltip(collapsed && !drawer, 'Personal workspace', <span className="avatar" aria-label="Personal workspace">J</span>)}
        {maybeTooltip(collapsed && !drawer, 'Refresh', <Button variant="ghost" size="sm" disabled={refreshDisabled} onClick={onRefresh}><RefreshCw size={14} className={refreshing ? 'spin' : ''}/><span className="rail-label">Refresh</span></Button>)}
      </div>
    </div>
    {!drawer && <Button variant="ghost" size="sm" className="rail-collapse-toggle" aria-expanded={!collapsed} onClick={onToggleCollapse}>
      {collapsed ? <ChevronsRight size={16}/> : <ChevronsLeft size={16}/>}<span className="rail-label">Collapse</span>
    </Button>}
  </nav>
}

function themeLabel(theme: Theme) {
  return theme === 'system' ? 'Theme: system' : theme === 'dark' ? 'Theme: dark' : 'Theme: light'
}

function maybeTooltip(wrap: boolean, label: string, element: React.ReactElement<{ 'aria-describedby'?: string }>) {
  return wrap ? <Tooltip label={label}>{element}</Tooltip> : element
}
