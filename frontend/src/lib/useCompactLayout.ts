import { useEffect, useState } from 'react'

// Matches the breakpoint in `index.css` where the sidebar gives way. Only one
// of the two navigations is mounted, so a hidden one never holds keyboard focus.
const QUERY = '(max-width:900px)'

export function useCompactLayout(): boolean {
  const [compact, setCompact] = useState(() => window.matchMedia(QUERY).matches)
  useEffect(() => {
    const media = window.matchMedia(QUERY)
    const update = () => setCompact(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  return compact
}
