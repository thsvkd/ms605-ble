import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { Dialog } from './Dialog'
import { NameSensorForm } from './NameSensorForm'

/** The dashboard's way to name an unregistered sensor; the user asked, so the alias field gets focus. */
export function NameSensorDialog({ deviceId, onClose }: { deviceId: string | null; onClose: () => void }) {
  const t = useStrings()
  const sensor = useStore((s) => (deviceId ? s.sensors[deviceId] : undefined))
  const open = deviceId !== null
  return (
    <Dialog open={open} title={t.form.nameAction} onClose={onClose}>
      {deviceId && sensor && (
        <NameSensorForm deviceId={deviceId} bleName={sensor.live?.name ?? null} autoFocus onDone={onClose} />
      )}
    </Dialog>
  )
}
