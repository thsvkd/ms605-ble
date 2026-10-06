import { scopeChangedCount, scopeOf, useDrafts } from '../store/drafts'
import { useStrings } from '../strings'
import { Button } from './Button'
import { Dialog } from './Dialog'
import styles from './ui.module.css'

/** Opens when the guard held a navigation (15.9.3): 머무르기 (default, Esc) or 버리고 이동. */
export function UnsavedChangesDialog() {
  const t = useStrings()
  const pending = useDrafts((s) => s.pendingNav)
  const count = useDrafts((s) => {
    const scope = scopeOf(window.location.pathname)
    return scope === null ? 0 : scopeChangedCount(s, scope)
  })
  const stay = () => useDrafts.setState({ pendingNav: null })
  return (
    <Dialog open={pending !== null} title={t.guard.title} onClose={stay}>
      <p className={styles.dialogText}>{t.guard.body(count)}</p>
      <div className={styles.dialogActions}>
        <Button onClick={stay} autoFocus>
          {t.guard.stay}
        </Button>
        <Button
          variant="dangerSolid"
          onClick={() => {
            const go = pending?.go
            useDrafts.setState({ pendingNav: null })
            go?.()
          }}
        >
          {t.guard.discard}
        </Button>
      </div>
    </Dialog>
  )
}
