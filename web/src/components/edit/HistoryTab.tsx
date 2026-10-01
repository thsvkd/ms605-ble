import { useMyApply } from '../../hooks/useMyApply'
import { sensorScope } from '../../store/drafts'
import { selectApplyMembers, selectBatchMembers, useStore } from '../../store/store'
import { ApplyResults } from './ApplyResults'
import { CalibrationHistoryList } from './CalibrationHistoryList'
import { DeviceHistoryPanel } from './DeviceHistoryPanel'
import styles from './edit.module.css'
import { RollbackPicker } from './RollbackPicker'

/** 이력: calibration records, settings backups (roll back to any), device records (실험적). Shown without a connection. */
export function HistoryTab({ deviceId }: { deviceId: string }) {
  const connected = useStore((s) => s.sensors[deviceId]?.live?.link === 'connected')
  const locked = useStore((s) => selectBatchMembers(s).has(deviceId) || selectApplyMembers(s).has(deviceId))
  const lastCalibration = useStore((s) => s.sensors[deviceId]?.last_calibration?.timestamp)
  const mine = useMyApply(sensorScope(deviceId))
  return (
    <div className={styles.main}>
      {mine.job && <ApplyResults job={mine.job} only={deviceId} onDismiss={mine.dismiss} onStarted={mine.start} />}
      <CalibrationHistoryList deviceId={deviceId} refresh={lastCalibration} />
      <RollbackPicker deviceId={deviceId} connected={connected} locked={locked} onStarted={mine.start} />
      <DeviceHistoryPanel deviceId={deviceId} connected={connected} />
    </div>
  )
}
