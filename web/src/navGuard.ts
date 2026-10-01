// Unsaved-changes guard (docs/GUI_API.md 15.9.3): wouter 3.13 routes every in-app navigation
// (Link, useLocation's navigate, Redirect) through Router's aroundNav. popstate does not pass here:
// the draft stays in the store and the editor draws it again on return (G30).
import { useEffect } from 'react'
import type { NavigateOptions } from 'wouter'
import { isScopeDirty, scopeOf, useDrafts } from './store/drafts'

export function guardNav(
  navigate: (to: string, o?: NavigateOptions) => void,
  to: string,
  options?: NavigateOptions,
): void {
  const s = useDrafts.getState()
  const from = scopeOf(window.location.pathname)
  if (from !== null && from !== scopeOf(to) && isScopeDirty(s, from)) {
    useDrafts.setState({
      pendingNav: {
        to,
        options,
        go: () => {
          useDrafts.getState().discard(from, true) // left the editor: no old base kept for the return
          navigate(to, options)
        },
      },
    })
    return
  }
  navigate(to, options)
}

/** Reload or tab close with any changed scope: the browser's own "leave site?" prompt. */
export function useBeforeUnloadGuard(): void {
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      const s = useDrafts.getState()
      const scopes = [...Object.keys(s.sensors).map((id) => `sensor:${id}`), 'bulk']
      if (!scopes.some((scope) => isScopeDirty(s, scope))) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])
}
