import { CheckCircle2, ChevronRight, Sparkles, Tag } from 'lucide-react'
import { memo, useEffect, useRef, useState } from 'react'
import { Link } from 'wouter'
import { sensorDisplayName } from '../alias'
import { useNow } from '../hooks/useNow'
import { sensorStatus } from '../status'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { BatteryIndicator } from './BatteryIndicator'
import { Button } from './Button'
import { type NameResult, NameSensorForm } from './NameSensorForm'
import { StatusBadge } from './StatusBadge'
import styles from './gather.module.css'

const FRESH_MS = 5000

interface Props {
  deviceId: string
  /** Arrived after the list was first drawn: highlight it. */
  arrived: boolean
  expanded: boolean
  onExpand: (id: string, focus: boolean) => void
  onCollapse: (id: string) => void
  focusName: boolean
}

/** Highlight for 5 s whenever this sensor (re)arrives — "the one you just pressed" (9.4). */
function useFresh(gatheredAt: number | undefined, arrived: boolean): boolean {
  const first = useRef(gatheredAt)
  const [fresh, setFresh] = useState(arrived)
  useEffect(() => {
    if (gatheredAt === first.current && !arrived) return
    first.current = gatheredAt
    setFresh(true)
    const timer = setTimeout(() => setFresh(false), FRESH_MS)
    return () => clearTimeout(timer)
  }, [gatheredAt, arrived])
  return fresh
}

export const GatheredItem = memo(function GatheredItem(props: Props) {
  const t = useStrings()
  const { deviceId, arrived, expanded, onExpand, onCollapse, focusName } = props
  const sensor = useStore((s) => s.sensors[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const now = useNow()
  const fresh = useFresh(sensor?.live?.gathered_at, arrived)
  const [message, setMessage] = useState<NameResult | null>(null)
  if (!sensor?.live) return null

  const status = sensorStatus(sensor, gathering, now)
  const reg = sensor.registry
  const bleName = sensorDisplayName(sensor, deviceId)
  const className = [styles.item, reg ? '' : styles.itemNew, fresh ? styles.fresh : ''].join(' ')

  const done = (result?: NameResult) => {
    onCollapse(deviceId)
    if (result) setMessage(result)
  }

  if (reg) {
    return (
      <li className={className}>
        <div className={styles.itemRow}>
          <span className={styles.itemName}>{reg.alias}</span>
          {reg.location && <span className={styles.itemLocation}>{reg.location}</span>}
          <span className={styles.itemEnd}>
            <StatusBadge status={status} />
            <span className={styles.registered}>
              <CheckCircle2 size={14} aria-hidden />
              {t.sensor.registered}
            </span>
          </span>
        </div>
        {status.hint && <p className={styles.itemMessage}>{status.hint}</p>}
        {message && (
          <p className={styles.itemMessage} role="status">
            {message === 'saved' ? t.form.saved : t.form.takenElsewhere}
          </p>
        )}
      </li>
    )
  }

  return (
    <li className={className}>
      <div className={styles.itemRow}>
        <NewBadge />
        <span className={`${styles.itemName} mono`}>{bleName}</span>
        <span className={styles.itemEnd}>
          <StatusBadge status={status} />
          <BatteryIndicator sensor={sensor} />
        </span>
      </div>
      {status.hint && <p className={styles.itemMessage}>{status.hint}</p>}
      {message && (
        <p className={styles.itemMessage} role="status">
          {message === 'saved' ? t.form.saved : t.form.takenElsewhere}
        </p>
      )}
      {expanded ? (
        <>
          <p className={styles.nameHint}>{t.sensor.nameIt}</p>
          <NameSensorForm
            deviceId={deviceId}
            bleName={bleName}
            autoFocus={focusName}
            onDone={done}
          />
        </>
      ) : (
        <div className={styles.itemRow}>
          <Button icon={Tag} onClick={() => onExpand(deviceId, true)}>
            {t.form.nameAction}
          </Button>
          <Link href={`/sensors/${deviceId}`} className={`${styles.itemLink} ${styles.itemEnd}`}>
            {t.detail.info}
            <ChevronRight size={16} aria-hidden />
          </Link>
        </div>
      )}
    </li>
  )
})

function NewBadge() {
  const t = useStrings()
  return (
    <span className={styles.newBadge}>
      <Sparkles size={12} aria-hidden />
      {t.sensor.new}
    </span>
  )
}
