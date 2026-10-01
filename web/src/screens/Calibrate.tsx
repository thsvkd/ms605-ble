import { useMemo, useState } from 'react'
import { useSearch } from 'wouter'
import { BatchProgress } from '../components/calibrate/BatchProgress'
import { BatchResults } from '../components/calibrate/BatchResults'
import styles from '../components/calibrate/calibrate.module.css'
import { PreflightStep } from '../components/calibrate/PreflightStep'
import { SelectStep, selectable } from '../components/calibrate/SelectStep'
import { defaultChoice, type StartChoice } from '../components/calibrate/StartOptions'
import { type CalibStep, StepIndicator } from '../components/calibrate/StepIndicator'
import { selectApplyMembers, selectBatchActive, selectSessions, useStore } from '../store/store'
import { t } from '../strings'
import { idsParam } from './Monitor'

const DISMISSED_KEY = 'ms605.dismissedBatch'

function readDismissed(): string | null {
  try {
    return sessionStorage.getItem(DISMISSED_KEY)
  } catch {
    return null
  }
}

function writeDismissed(batchId: string): void {
  try {
    sessionStorage.setItem(DISMISSED_KEY, batchId)
  } catch {
    // this tab only, in memory
  }
}

/**
 * 14.8.6: the server's batch picks the view (any screen sees and controls the same batch);
 * only the select/check steps before a start are this screen's own state.
 */
export function CalibrateScreen() {
  const batch = useStore((s) => s.batch)
  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const gathering = useStore((s) => s.gather.gathering)
  const search = useSearch()
  const sessions = useMemo(() => selectSessions({ sensors, sites }), [sensors, sites])
  const apply = useStore((s) => s.apply)
  const applyLocked = useMemo(() => selectApplyMembers({ apply }), [apply])

  const [dismissed, setDismissed] = useState(readDismissed)
  const [checked, setChecked] = useState<ReadonlySet<string>>(() => new Set(idsParam(search) ?? []))
  const [checkIds, setCheckIds] = useState<string[] | null>(null) // fixed on entering the check step
  const [choice, setChoice] = useState<StartChoice>(() => defaultChoice(new Date()))
  const [retryChoice, setRetryChoice] = useState<StartChoice>(() => defaultChoice(new Date(), 'delay'))

  const active = selectBatchActive({ batch })
  const showResults = batch !== null && !active && batch.batch_id !== dismissed
  const view: CalibStep = active ? 2 : showResults ? 3 : checkIds ? 1 : 0

  const toggle = (id: string) => {
    const next = new Set(checked)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setChecked(next)
  }

  const next = () => {
    // the selection order follows the list order, which the batch keeps (device_ids, 14.4)
    setCheckIds(sessions.filter((s) => selectable(s, applyLocked) && checked.has(s.device_id)).map((s) => s.device_id))
  }

  const startOver = () => {
    if (batch) {
      writeDismissed(batch.batch_id)
      setDismissed(batch.batch_id)
    }
    setCheckIds(null)
  }

  return (
    <div className={styles.screen}>
      <h1 className={styles.title}>{t.calib.title}</h1>
      <StepIndicator current={view} />
      {active && batch ? (
        <BatchProgress batch={batch} sensors={sensors} gathering={gathering} />
      ) : showResults && batch ? (
        <BatchResults
          batch={batch}
          sensors={sensors}
          gathering={gathering}
          retryChoice={retryChoice}
          onRetryChoice={setRetryChoice}
          onNew={startOver}
        />
      ) : checkIds ? (
        <PreflightStep
          ids={checkIds}
          sensors={sensors}
          choice={choice}
          onChoice={setChoice}
          onBack={() => setCheckIds(null)}
        />
      ) : (
        <SelectStep
          sessions={sessions}
          checked={checked}
          gathering={gathering}
          onToggle={toggle}
          applyLocked={applyLocked}
          onSelectAll={() =>
            setChecked(new Set(sessions.filter((s) => selectable(s, applyLocked)).map((s) => s.device_id)))
          }
          onNext={next}
        />
      )}
    </div>
  )
}
