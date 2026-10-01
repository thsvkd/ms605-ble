import { Eye } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { useLocation, useSearch } from 'wouter'
import { applyDraft, clone as cloneApply, failureText, previewClone, previewDraft } from '../api/client'
import type { CloneIn, ConfigView, DraftIn, DraftPreview } from '../api/types'
import { Button } from '../components/Button'
import cal from '../components/calibrate/calibrate.module.css'
import { ApplyResults } from '../components/edit/ApplyResults'
import { BulkTargets, targetReason } from '../components/edit/BulkTargets'
import { BulkThresholds } from '../components/edit/BulkThresholds'
import { CloneSetup } from '../components/edit/CloneSetup'
import { CommonSettings } from '../components/edit/CommonSettings'
import { DiffPreview } from '../components/edit/DiffPreview'
import styles from '../components/edit/edit.module.css'
import {
  type BulkDraft,
  bulkToDraftIn,
  CLONE_DEFAULT_SECTIONS,
  type CloneDraft,
  clearBulkValues,
  emptyBulk,
  expectRevOf,
} from '../draft'
import { DESKTOP, useMediaQuery } from '../hooks/useMediaQuery'
import { useMyApply } from '../hooks/useMyApply'
import { useDrafts } from '../store/drafts'
import { selectApplyMembers, selectBatchMembers, selectSessions, sensorName, useStore } from '../store/store'
import { t } from '../strings'
import { idsParam } from './Monitor'

const DISMISSED_KEY = 'ms605.dismissedApply'

function readDismissed(): string | null {
  try {
    return sessionStorage.getItem(DISMISSED_KEY)
  } catch {
    return null
  }
}

function writeDismissed(applyId: string): void {
  try {
    sessionStorage.setItem(DISMISSED_KEY, applyId)
  } catch {
    // this tab only, in memory
  }
}

type Previewed = { kind: 'edit'; body: DraftIn; data: DraftPreview } | { kind: 'clone'; body: CloneIn; data: DraftPreview }

/**
 * 일괄 편집 / 복제 (15.9.9-15.9.10). Stages, first match: this client's (or a running) job -> results;
 * a preview -> the diff; else the form. Thresholds are relative by default (D8, G32).
 */
export function BulkEditScreen() {
  const search = useSearch()
  const [, navigate] = useLocation()
  const params = new URLSearchParams(search)
  const mode = params.get('mode') === 'clone' ? 'clone' : 'edit'
  const sourceParam = params.get('source')
  const modeName = useId()

  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const gathering = useStore((s) => s.gather.gathering)
  const batch = useStore((s) => s.batch)
  const job = useStore((s) => s.apply)
  const sessions = useMemo(() => selectSessions({ sensors, sites }), [sensors, sites])
  const batchLocked = useMemo(() => selectBatchMembers({ batch }), [batch])
  const applyLocked = useMemo(() => selectApplyMembers({ apply: job }), [job])
  const desktop = useMediaQuery(DESKTOP)

  const bulk = useDrafts((s) => s.bulk)
  const cloneDraft = useDrafts((s) => s.clone)
  const { setBulk, setClone } = useDrafts.getState()
  const mine = useMyApply('bulk')

  const [dismissed, setDismissed] = useState(readDismissed)
  const [previewed, setPreviewed] = useState<Previewed | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [source, setSource] = useState<ConfigView | null>(null)

  // first visit: ?ids= is the initial selection; ?source= picks the clone source (from a sensor's 설정 tab)
  useEffect(() => {
    const s = useDrafts.getState()
    if (!s.bulk) setBulk(emptyBulk(idsParam(search) ?? []))
    const c = s.clone
    if (!c) setClone({ source: sourceParam, sections: [...CLONE_DEFAULT_SECTIONS], ids: [], ack: false })
    else if (sourceParam && c.source !== sourceParam)
      setClone({ ...c, source: sourceParam, ids: c.ids.filter((x) => x !== sourceParam) })
  }, [search, sourceParam, setBulk, setClone])

  const names = useMemo(
    () => Object.fromEntries(Object.values(sensors).map((s) => [s.device_id, sensorName(s, s.device_id)])),
    [sensors],
  )
  const usable = (ids: readonly string[], exclude?: string | null) =>
    sessions
      .filter((s) => s.device_id !== exclude && ids.includes(s.device_id))
      .filter((s) => targetReason(s, batchLocked, applyLocked) === null)
      .map((s) => s.device_id)

  const showResults =
    job !== null &&
    job.apply_id !== dismissed &&
    (job.state === 'running' || mine.job?.apply_id === job.apply_id)

  const setMode = (m: 'edit' | 'clone') => {
    setPreviewed(null)
    setError(null)
    navigate(m === 'clone' ? '/bulk?mode=clone' : '/bulk', { replace: true })
  }

  const run = async (f: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await f()
    } catch (e) {
      setError(failureText(e))
    } finally {
      setBusy(false)
    }
  }

  const previewEdit = (b: BulkDraft, targets: string[]) =>
    run(async () => {
      const req = bulkToDraftIn(b)
      if (!req) return
      const body = { ...req, targets }
      setPreviewed({ kind: 'edit', body, data: await previewDraft(body) })
    })

  const cloneSections = (c: CloneDraft) =>
    c.sections.filter((s) => !(s === 'dnd' && source?.profile.dnd === null))

  const previewCloneNow = (c: CloneDraft, targets: string[]) =>
    run(async () => {
      if (!c.source) return
      const body: CloneIn = { source: c.source, targets, sections: cloneSections(c), expect_rev: null }
      setPreviewed({ kind: 'clone', body, data: await previewClone(body) })
    })

  if (!bulk || !cloneDraft) return null

  // -- stage: results ------------------------------------------------------------------------------
  if (showResults && job) {
    return (
      <div className={styles.bulk}>
        <h1 className={cal.title}>{t.bulk.title}</h1>
        <ApplyResults
          job={job}
          dismissLabel={t.bulk.newDraft}
          onStarted={mine.start}
          onDismiss={() => {
            writeDismissed(job.apply_id)
            setDismissed(job.apply_id)
            mine.dismiss()
          }}
        />
      </div>
    )
  }

  // -- stage: diff ------------------------------------------------------------------------------------
  if (previewed) {
    const p = previewed
    const n = p.data.items.length
    const back = () => {
      setPreviewed(null)
      setError(null) // a failed re-preview's alert belongs to the diff, not the form
    }
    return (
      <div className={styles.bulk}>
        <h1 className={cal.title}>{t.bulk.title}</h1>
        <DiffPreview
          preview={p.data}
          names={names}
          title={p.kind === 'clone' ? t.clone.from(names[p.body.source] ?? p.body.source) : undefined}
          applyLabel={p.kind === 'clone' ? t.clone.applyN(n) : t.bulk.applyN(n)}
          ackInitially={p.kind === 'edit' && bulk.absoluteAck}
          onBack={back}
          dropping={busy}
          onDropErrors={(drop) => {
            const keep = p.data.items.map((i) => i.device_id).filter((id) => !drop.includes(id))
            if (p.kind === 'edit') {
              setBulk({ ...bulk, ids: bulk.ids.filter((id) => !drop.includes(id)) })
              void previewEdit(bulk, keep)
            } else {
              setClone({ ...cloneDraft, ids: cloneDraft.ids.filter((id) => !drop.includes(id)) })
              void previewCloneNow(cloneDraft, keep)
            }
          }}
          onApply={async () => {
            const expect = expectRevOf(p.data)
            if (p.kind === 'edit') {
              mine.start(await applyDraft({ ...p.body, expect_rev: expect }))
              setBulk(clearBulkValues(useDrafts.getState().bulk ?? bulk)) // G37: values go, the selection stays
            } else {
              const rev = p.data.source_rev === null ? {} : { [p.body.source]: p.data.source_rev } // the previewed read
              mine.start(await cloneApply({ ...p.body, expect_rev: { ...expect, ...rev } }))
              setClone({ ...cloneDraft, ids: [] })
            }
            setPreviewed(null)
          }}
        />
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </div>
    )
  }

  // -- stage: form ----------------------------------------------------------------------------------
  const edit = mode === 'edit'
  const targets = edit ? usable(bulk.ids) : usable(cloneDraft.ids, cloneDraft.source)
  const changes = edit ? bulkToDraftIn(bulk) !== null : cloneSections(cloneDraft).length > 0
  const why = edit
    ? targets.length === 0
      ? t.bulk.needTargets
      : !changes
        ? t.bulk.needChange
        : bulk.mode === 'absolute' && !bulk.absoluteAck
          ? t.bulk.needAck
          : null
    : !cloneDraft.source
      ? t.clone.pickSource
      : !changes
        ? t.clone.needSections
        : targets.length === 0
          ? t.bulk.needTargets
          : null

  const primary = (
    <div className={cal.dock}>
      <div className={cal.dockInner}>
        {why && <p className={styles.reason}>{why}</p>}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        <Button
          variant="primary"
          size="lg"
          block
          icon={Eye}
          disabled={why !== null || busy}
          onClick={() => (edit ? previewEdit(bulk, targets) : previewCloneNow(cloneDraft, targets))}
        >
          {t.bulk.previewN(targets.length)}
        </Button>
      </div>
    </div>
  )

  const set = (b: BulkDraft) => {
    setError(null)
    setBulk(b)
  }

  return (
    <div className={styles.bulk}>
      <h1 className={cal.title}>{t.bulk.title}</h1>
      <fieldset className={cal.segmented}>
        <legend className="visually-hidden">{t.bulk.modeLabel}</legend>
        {(['edit', 'clone'] as const).map((m) => (
          <label key={m} className={cal.segment}>
            <input type="radio" name={modeName} value={m} checked={mode === m} onChange={() => setMode(m)} />
            {m === 'edit' ? t.bulk.modeEdit : t.bulk.modeClone}
          </label>
        ))}
      </fieldset>

      {edit ? (
        <div className={styles.bulkGrid}>
          <div className={styles.bulkCol}>
            <BulkTargets
              sessions={sessions}
              selected={bulk.ids}
              batchLocked={batchLocked}
              applyLocked={applyLocked}
              gathering={gathering}
              onChange={(ids) => set({ ...bulk, ids })}
            />
            {desktop && <CommonSettings bulk={bulk} onChange={set} />}
          </div>
          <div className={styles.bulkCol}>
            <BulkThresholds bulk={bulk} onChange={set} />
            {!desktop && <CommonSettings bulk={bulk} onChange={set} />}
            {primary}
          </div>
        </div>
      ) : (
        <div className={styles.bulkCol}>
          <CloneSetup
            clone={cloneDraft}
            sessions={sessions}
            batchLocked={batchLocked}
            applyLocked={applyLocked}
            gathering={gathering}
            onChange={(c) => {
              setError(null)
              setClone(c)
            }}
            onSource={setSource}
          />
          {primary}
        </div>
      )}
    </div>
  )
}
