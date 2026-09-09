import { useRef } from 'react'
import { ChevronsLeft, ChevronsRight, Layers3 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/Tooltip'
import { railSections, tabLabel, tabMeta, type RailSection } from '@/lib/tabs'

function sectionId(name: string) { return `rail-section-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}` }

export function NavigationRail({ tab, onSelect, reviewCount, proposalCount, collapsed, onToggleCollapse, ledgerLabel, drawer = false }: {
  tab: string
  onSelect: (tab: string) => void
  reviewCount: number
  proposalCount: number
  collapsed: boolean
  onToggleCollapse: () => void
  ledgerLabel: string
  drawer?: boolean
}) {
  const buttonRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const flatTabs = railSections.flatMap((s: RailSection) => s.tabs)

  function focusAt(index: number) {
    const clamped = Math.max(0, Math.min(flatTabs.length - 1, index))
    buttonRefs.current[flatTabs[clamped]]?.focus()
  }
  function onKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === 'ArrowDown') { event.preventDefault(); focusAt(index + 1) }
    else if (event.key === 'ArrowUp') { event.preventDefault(); focusAt(index - 1) }
    else if (event.key === 'Home') { event.preventDefault(); focusAt(0) }
    else if (event.key === 'End') { event.preventDefault(); focusAt(flatTabs.length - 1) }
  }

  return <nav className={`nav-rail${collapsed && !drawer ? ' is-collapsed' : ''}`} aria-label="Main navigation" id="main-navigation">
    <a className="rail-brand" href="?view=Overview" onClick={e => { e.preventDefault(); onSelect('Overview') }}>
      <span className="brand-icon"><Layers3 size={19}/></span>
      <span className="rail-brand-label"><span className="rail-brand-name">fincl</span><span className="ledger-name">{ledgerLabel}</span></span>
    </a>
    <div className="rail-sections">
      {railSections.map(section => {
        const headingId = sectionId(section.name)
        return <div className="rail-section" key={section.name}>
          <p className="rail-section-label" id={headingId}>{section.name}</p>
          <ul aria-labelledby={headingId}>
            {section.tabs.map(t => {
              const meta = tabMeta[t]
              const Icon = meta.icon
              const active = tab === t
              const index = flatTabs.indexOf(t)
              const count = t === 'Transactions' ? reviewCount : t === 'Review' ? proposalCount : 0
              const button = <button
                ref={el => { buttonRefs.current[t] = el }}
                key={t}
                aria-current={active ? 'page' : undefined}
                tabIndex={active || (!flatTabs.includes(tab) && index === 0) ? 0 : -1}
                onClick={() => onSelect(t)}
                onKeyDown={e => onKeyDown(e, index)}
              >
                {Icon && <Icon size={17}/>}
                <span className="rail-label">{tabLabel(t)}</span>
                {count > 0 && <span className="nav-count">{count}</span>}
              </button>
              return <li key={t}>{collapsed && !drawer ? <Tooltip label={tabLabel(t)}>{button}</Tooltip> : button}</li>
            })}
          </ul>
        </div>
      })}
    </div>
    {!drawer && <Button variant="ghost" size="sm" className="rail-collapse-toggle" aria-expanded={!collapsed} onClick={onToggleCollapse}>
      {collapsed ? <ChevronsRight size={16}/> : <ChevronsLeft size={16}/>}<span className="rail-label">Collapse</span>
    </Button>}
  </nav>
}
