import { Eye, RotateCcw } from 'lucide-react'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import styles from './edit.module.css'

interface Props {
  count: number
  busy: boolean
  disabled: boolean
  onReset: () => void
  onPreview: () => void
}

/** Shown only while something changed: the count, 모두 되돌리기 (draft only, no confirm), and the one primary: 미리보기. */
export function DraftBar({ count, busy, disabled, onReset, onPreview }: Props) {
  const t = useStrings()
  return (
    <div className={styles.draftBar}>
      <div className={styles.draftBarInner} role="group" aria-label={t.edit.changed(count)}>
        <span className={styles.draftCount}>{t.edit.changed(count)}</span>
        <Button variant="ghost" icon={RotateCcw} onClick={onReset} disabled={busy}>
          {t.edit.resetAll}
        </Button>
        <Button variant="primary" icon={Eye} onClick={onPreview} disabled={busy || disabled}>
          {t.edit.preview}
        </Button>
      </div>
    </div>
  )
}
