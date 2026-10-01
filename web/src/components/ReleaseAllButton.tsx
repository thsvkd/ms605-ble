import { Unplug } from 'lucide-react'
import { useState } from 'react'
import { release } from '../api/client'
import { useStore } from '../store/store'
import { t } from '../strings'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'

/** Secondary; shown only while some session exists. Releasing everything also stops gathering (6.4). */
export function ReleaseAllButton({ className }: { className?: string }) {
  const hasSession = useStore((s) => Object.values(s.sensors).some((x) => x.live !== null))
  const gathering = useStore((s) => s.gather.gathering)
  const [open, setOpen] = useState(false)
  if (!hasSession) return null
  return (
    <>
      <Button icon={Unplug} className={className} onClick={() => setOpen(true)}>
        {t.release.all}
      </Button>
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
