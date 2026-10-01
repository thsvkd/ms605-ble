import { Unplug } from 'lucide-react'
import { useState } from 'react'
import { release } from '../api/client'
import { selectBatchActive, useStore } from '../store/store'
import { t } from '../strings'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import styles from './ui.module.css'

/** Secondary; shown only while some session exists. Releasing everything also stops gathering (6.4). */
export function ReleaseAllButton({ className }: { className?: string }) {
  const hasSession = useStore((s) => Object.values(s.sensors).some((x) => x.live !== null))
  const gathering = useStore((s) => s.gather.gathering)
  const locked = useStore(selectBatchActive) // a calibrating link must not drop (G22)
  const [open, setOpen] = useState(false)
  if (!hasSession) return null
  return (
    <>
      <Button
        icon={Unplug}
        className={className}
        disabled={locked}
        title={locked ? t.release.blockedByBatch : undefined}
        aria-describedby={locked ? 'release-all-locked' : undefined}
        onClick={() => setOpen(true)}
      >
        {t.release.all}
      </Button>
      {locked && (
        <span id="release-all-locked" className={styles.note}>
          {t.release.blockedByBatch}
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
