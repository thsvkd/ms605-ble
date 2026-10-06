import { AlertTriangle, CircleSlash, Loader2, Lock, RefreshCw } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useState } from 'react'
import { Link } from 'wouter'
import { applyDraft, failureText, getConfig, previewDraft } from '../../api/client'
import type { DraftPreview } from '../../api/types'
import { changedCount, expectRevOf, isDirty, type SensorDraft, toDraftIn } from '../../draft'
import { DESKTOP, useMediaQuery } from '../../hooks/useMediaQuery'
import { useMyApply } from '../../hooks/useMyApply'
import { useNow } from '../../hooks/useNow'
import { sensorStatus } from '../../status'
import { sensorScope, useDrafts } from '../../store/drafts'
import { selectApplyMembers, selectBatchMembers, sensorName, useStore } from '../../store/store'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import { Dialog } from '../Dialog'
import { ApplyResults } from './ApplyResults'
import { DiffPreview } from './DiffPreview'
import { DraftBar } from './DraftBar'
import styles from './edit.module.css'

type Update = (f: (d: SensorDraft) => SensorDraft) => void

interface Props {
  deviceId: string
  children: (draft: SensorDraft, disabled: boolean, update: Update) => ReactNode
}

/**
 * The 설정 and 고급 tabs share one draft (`sensor:<id>`) and this frame (15.9.6-15.9.7): the state table
 * (connection, calibration and apply locks, loading), the draft bar, the server's diff, apply, and
 * the result of this client's job. Nothing reaches the sensor before 적용 on a diff.
 */
export function EditFrame({ deviceId, children }: Props) {
  const t = useStrings()
  const scope = sensorScope(deviceId)
  const sensor = useStore((s) => s.sensors[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const batchLocked = useStore((s) => selectBatchMembers(s).has(deviceId))
  const applyLocked = useStore((s) => selectApplyMembers(s).has(deviceId))
  const running = useStore((s) => s.apply)
  const draft = useDrafts((s) => s.sensors[deviceId])
  const mineRec = useDrafts((s) => s.mine[scope])
  const { openSensor, rebase, discard, updateSensor, setMine } = useDrafts.getState()
  const mine = useMyApply(scope)
  const desktop = useMediaQuery(DESKTOP)
  const now = useNow()

  const [loadError, setLoadError] = useState<{ cause: unknown } | null>(null)
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState<{ for: string; data: DraftPreview } | null>(null)
  const [previewError, setPreviewError] = useState<{ cause: unknown } | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)

  const connected = sensor?.live?.link === 'connected'
  const locked = batchLocked || applyLocked
  const request = draft ? JSON.stringify(toDraftIn(draft)) : ''
  // a preview belongs to the request and the base it was computed against: a rebase (even one that
  // keeps every edit) asks the server again, so the before values, risks and config_rev are current
  const previewKey = draft ? `${request}@${draft.base.read_at}` : ''

  const load = useCallback(
    async (how: 'open' | 'rebase') => {
      setLoading(true)
      setLoadError(null)
      try {
        const base = await getConfig(deviceId)
        if (how === 'open') useDrafts.getState().openSensor(base)
        else useDrafts.getState().rebase(base)
      } catch (e) {
        setLoadError({ cause: e })
      } finally {
        setLoading(false)
      }
    },
    [deviceId],
  )

  const needLoad = connected && !locked && !draft && !loadError
  useEffect(() => {
    if (needLoad) void load('open')
  }, [needLoad, load])

  // The sensor was gathered again after the draft's base was read (it dropped and came back, or the
  // server restarted): anything may have changed it meanwhile, and config_rev does not see that.
  const regathered = connected && !locked && !!draft && (sensor?.live?.gathered_at ?? 0) > draft.base.read_at
  useEffect(() => {
    if (regathered) void load('rebase')
  }, [regathered, load])

  // my job ended for this sensor (15.9.2): a verified apply clears the draft; anything else keeps it.
  // Both read the device again so the base is what is on it now.
  useEffect(() => {
    if (!mineRec || mineRec.settled || running?.apply_id !== mineRec.applyId) return
    const item = running.items.find((i) => i.device_id === deviceId)
    if (!item || item.state === 'queued' || item.state === 'applying') return
    setMine(scope, { ...mineRec, settled: true })
    if (running.kind === 'apply' && item.state === 'verified') discard(scope)
    getConfig(deviceId).then(
      (base) => (useDrafts.getState().sensors[deviceId] ? rebase(base) : openSensor(base)),
      () => {}, // the rev banner offers 새 값 불러오기
    )
  }, [running, mineRec, deviceId, scope, setMine, discard, rebase, openSensor])

  if (!sensor || !connected) {
    const status = sensor ? sensorStatus(sensor, gathering, now) : null
    return (
      <div className={styles.empty}>
        <CircleSlash size={32} aria-hidden />
        <p>{t.edit.needConnection}</p>
        {status?.hint && <p className={styles.emptyHint}>{status.hint}</p>}
        <Link href={`/sensors/${encodeURIComponent(deviceId)}/history`} className={styles.link}>
          {t.detail.tabHistory}
        </Link>
      </div>
    )
  }

  const update: Update = (f) => {
    setPreviewError(null)
    updateSensor(deviceId, f)
  }

  const myResults = mine.job && mine.job.items.some((i) => i.device_id === deviceId) && !applyLocked && (
    <ApplyResults job={mine.job} only={deviceId} onDismiss={mine.dismiss} onStarted={mine.start} />
  )

  const lockNote = batchLocked ? (
    <p className={styles.lockNote} role="status">
      <Lock size={16} aria-hidden />
      {t.edit.lockedByBatch}
    </p>
  ) : applyLocked && running ? (
    <>
      <ApplyResults job={running} only={deviceId} onDismiss={() => {}} />
      <p className={styles.lockNote} role="status">
        <Loader2 size={16} className="spin" aria-hidden />
        {t.edit.lockedByApply}
      </p>
    </>
  ) : null

  if (!draft) {
    return (
      <div className={styles.frame}>
        <div className={styles.main}>
          {lockNote}
          {!lockNote &&
            (loadError ? (
              <div className={styles.section}>
                <p className={styles.error} role="alert">
                  {failureText(loadError.cause)}
                </p>
                <Button icon={RefreshCw} onClick={() => void load('open')} disabled={loading}>
                  {t.edit.reload}
                </Button>
              </div>
            ) : (
              <p className={styles.lockNote} role="status">
                <Loader2 size={16} className="spin" aria-hidden />
                {t.edit.loading}
              </p>
            ))}
        </div>
      </div>
    )
  }

  const dirty = isDirty(draft.edit)
  const revMoved = sensor.config_rev !== draft.base.config_rev && !applyLocked
  const shown = preview && preview.for === previewKey ? preview.data : null

  const openPreview = async () => {
    setPreviewBusy(true)
    setPreviewError(null)
    try {
      setPreview({ for: previewKey, data: await previewDraft(toDraftIn(draft)) })
    } catch (e) {
      setPreviewError({ cause: e })
    } finally {
      setPreviewBusy(false)
    }
  }

  const diff = shown && (
    <DiffPreview
      preview={shown}
      names={{ [deviceId]: sensorName(sensor, deviceId) }}
      applyLabel={t.diff.apply}
      sheet={!desktop}
      onBack={() => setPreview(null)}
      onApply={async () => {
        const job = await applyDraft({ ...toDraftIn(draft), expect_rev: expectRevOf(shown) })
        mine.start(job)
        setPreview(null)
      }}
    />
  )

  return (
    <div className={styles.frame}>
      <div className={styles.main}>
        {revMoved && (
          <div className={styles.banner} role="status">
            <span>
              <AlertTriangle size={16} aria-hidden />
              {t.edit.revChanged}
            </span>
            <Button icon={RefreshCw} onClick={() => void load('rebase')} disabled={loading}>
              {t.edit.rebase}
            </Button>
          </div>
        )}
        {lockNote}
        {!desktop && myResults}
        {children(draft, locked, update)}
      </div>
      <div className={styles.side}>
        {desktop && myResults}
        {dirty && !locked && (
          <DraftBar
            count={changedCount(draft)}
            busy={previewBusy}
            disabled={false}
            onReset={() => {
              discard(scope)
              void load('rebase') // a clean draft starts from what is on the device now
            }}
            onPreview={openPreview}
          />
        )}
        {previewError && (
          <p className={styles.error} role="alert">
            {failureText(previewError.cause)}
          </p>
        )}
        {desktop && diff}
      </div>
      {!desktop && (
        <Dialog open={Boolean(diff)} title={t.diff.title} onClose={() => setPreview(null)} className={styles.wideDialog}>
          {diff}
        </Dialog>
      )}
    </div>
  )
}
