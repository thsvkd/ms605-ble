import { ArrowLeft, Construction, Crosshair, Trash2, Unplug } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation } from 'wouter'
import { ApiRequestError, deleteSensor, release } from '../api/client'
import { Button, buttonClass } from '../components/Button'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EditSensorForm } from '../components/EditSensorForm'
import { LiveStrip } from '../components/LiveStrip'
import { NameSensorForm } from '../components/NameSensorForm'
import { SensorInfoList } from '../components/SensorInfoList'
import { StatusBadge } from '../components/StatusBadge'
import styles from '../components/detail.module.css'
import { useNow } from '../hooks/useNow'
import { sensorStatus } from '../status'
import { selectBatchMembers, useStore } from '../store/store'
import { errorText, t } from '../strings'

function BackLink() {
  return (
    <Link href="/" className={styles.back}>
      <ArrowLeft size={16} aria-hidden />
      {t.detail.back}
    </Link>
  )
}

export function SensorDetailScreen({ deviceId }: { deviceId: string }) {
  const sensor = useStore((s) => s.sensors[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const locked = useStore((s) => selectBatchMembers(s).has(deviceId))
  const now = useNow()
  const [, navigate] = useLocation()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!sensor) {
    return (
      <div className={styles.screen}>
        <BackLink />
        <div className={styles.empty}>
          <h1 className={styles.title}>{t.detail.notFound}</h1>
        </div>
      </div>
    )
  }

  const status = sensorStatus(sensor, gathering, now)
  const reg = sensor.registry
  const title = reg?.alias ?? sensor.live?.name ?? sensor.live?.address ?? deviceId

  const releaseOne = async () => {
    setError(null)
    try {
      await release([deviceId])
    } catch (e) {
      setError(e instanceof ApiRequestError ? errorText(e.code, e.message) : t.error.internal)
    }
  }

  return (
    <div className={styles.screen}>
      <BackLink />
      <div className={styles.head}>
        <h1 className={styles.title}>{title}</h1>
        <StatusBadge status={status} />
        {status.hint && <p className={styles.hint}>{status.hint}</p>}
      </div>

      {reg ? (
        <EditSensorForm key={deviceId} deviceId={deviceId} registry={reg} />
      ) : (
        <section className={styles.card} aria-labelledby="name-title">
          <h2 id="name-title" className={styles.cardTitle}>
            {t.sensor.nameIt}
          </h2>
          <NameSensorForm
            deviceId={deviceId}
            bleName={sensor.live?.name ?? null}
            onDone={(r) => r === undefined && navigate('/')}
          />
        </section>
      )}

      {sensor.live && <LiveStrip deviceId={deviceId} />}
      {sensor.live?.link === 'connected' && (
        <Link href={`/calibrate?ids=${encodeURIComponent(deviceId)}`} className={buttonClass('secondary')}>
          <Crosshair size={18} aria-hidden />
          {t.calib.thisSensor}
        </Link>
      )}

      <SensorInfoList sensor={sensor} />

      <p className={styles.soon}>
        <Construction size={20} aria-hidden />
        {t.detail.comingSoon}
      </p>

      {(sensor.live || reg) && (
        <div className={styles.dangerZone}>
          {sensor.live && (
            <Button
              icon={Unplug}
              onClick={releaseOne}
              disabled={locked}
              aria-describedby={locked ? 'release-locked' : undefined}
            >
              {t.release.one}
            </Button>
          )}
          {reg && (
            <Button variant="danger" icon={Trash2} onClick={() => setConfirmDelete(true)}>
              {t.delete.action}
            </Button>
          )}
        </div>
      )}
      {locked && (
        <p id="release-locked" className={styles.lockNote}>
          {t.release.blockedByBatch}
        </p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      <ConfirmDialog
        open={confirmDelete}
        title={t.delete.action}
        body={t.delete.confirm}
        confirmLabel={t.delete.action}
        danger
        onConfirm={async () => {
          await deleteSensor(deviceId)
          if (!sensor.live) navigate('/')
        }}
        onClose={() => setConfirmDelete(false)}
      />
    </div>
  )
}
