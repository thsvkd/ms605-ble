import { useEffect, useState } from 'react'

const isVisible = () => document.visibilityState === 'visible'

/** document.visibilityState === 'visible', kept current: a hidden tab should not hold live subscriptions. */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(isVisible)
  useEffect(() => {
    const update = () => setVisible(isVisible())
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}
