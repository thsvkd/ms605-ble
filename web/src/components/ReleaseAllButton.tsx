import { Unplug } from 'lucide-react'
import { useState } from 'react'
import { release } from '../api/client'
import { selectApplyActive, selectBatchActive, useStore } from '../store/store'
import { useStrings } from '../strings'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import styles from './ui.module.css'

/** Secondary; shown only while some session exists. Releasing everything also stops gathering (6.4). */
export function ReleaseAllButton({ className }: { className?: string }) {
  const t = useStrings()
  const hasSession = useStore((s) => Object.values(s.sensors).some((x) => x.live !== null))
  const gathering = useStore((s) => s.gather.gathering)
  const batchLocked = useStore(selectBatchActive) // a calibrating link must not drop (G22)
  const applyLocked = useStore(selectApplyActive) // nor one being written (G27)
  const locked = batchLocked || applyLocked
  const note = applyLocked ? t.apply.lockedRelease : t.release.blockedByBatch
  const [open, setOpen] = useState(false)
  if (!hasSession) return null
  return (
    <>
      <Button
        icon={Unplug}
        className={className}
        disabled={locked}
        title={locked ? note : undefined}
        aria-describedby={locked ? 'release-all-locked' : undefined}
        onClick={() => setOpen(true)}
      >
        {t.release.all}
      </Button>
      {locked && (
        <span id="release-all-locked" className={styles.note}>
          {note}
        </span>
      )}
      <ConfirmDialog
        open={open}
        title={t.release.all}
        body={gathering ? t.release.confirmGathering : t.release.confirm}
        confirmLabel={t.release.all}
        onConfirm={() => release(null)}
        onClose={() => setOpen(false)}
      />
    </>
  )
}
