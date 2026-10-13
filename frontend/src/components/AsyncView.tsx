import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'

// One loading/error/ready wrapper, replacing the load/error/retry/skeleton
// blocks that were hand-copied into ~12 views with drifting wording and
// markup (PLAN-ui-cohesion.md §1.4). `data` doubles as the readiness gate:
// while it is null the view shows a skeleton (loading) or a retry prompt
// (error), and `children` only runs once there is something to render, so
// call sites keep full type narrowing instead of a nullable prop.
export function AsyncView<T>({ loading, error, data, onRetry, errorTitle = 'We couldn’t load this.', loadingLabel = 'Loading…', skeletonRows = 3, children }: {
  loading: boolean
  error: string
  data: T | null | undefined
  onRetry: () => void
  errorTitle?: string
  loadingLabel?: string
  skeletonRows?: number
  children: (data: T) => ReactNode
}) {
  if (loading && data == null) {
    return <div className="skeleton-page" aria-label={loadingLabel}>{Array.from({ length: skeletonRows }, (_, i) => <div key={i}/>)}</div>
  }
  if (error && data == null) {
    return <div className="alert error" role="alert"><div><strong>{errorTitle}</strong><p>{error}</p></div><Button variant="outline" onClick={onRetry}>Try again</Button></div>
  }
  if (data == null) return null
  return <>{error && <div className="alert error" role="alert"><div><strong>{errorTitle}</strong><p>{error}</p><p>Showing the previously loaded view; these values have not been refreshed.</p></div><Button variant="outline" onClick={onRetry}>Try again</Button></div>}{children(data)}</>
}
