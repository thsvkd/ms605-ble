import { AlertTriangle, ArrowLeft, ChevronRight, Filter, Upload, XCircle } from 'lucide-react'
import { useId, useState } from 'react'
import { failureText } from '../../api/client'
import type { Change, DraftPreview, SensorPreview } from '../../api/types'
import { formatChange, needsOverwriteAck, riskText, rowLabel } from '../../apply'
import { t } from '../../strings'
import { Button } from '../Button'
import styles from './edit.module.css'
import cal from '../calibrate/calibrate.module.css'

interface Props {
  preview: DraftPreview
  names: Record<string, string>
  onApply: () => Promise<void>
  onBack: () => void
  applyLabel: string
  /** Heading; diff.title by default (clone and rollback name their source / point in time). */
  title?: string
  /** The bulk screen's own overwrite tick carries over. */
  ackInitially?: boolean
  /** Re-preview without the sensors that cannot be applied. */
  onDropErrors?: (ids: string[]) => void
  /** That re-preview is in flight: its button waits. */
  dropping?: boolean
  /** Inside a dialog (which carries the title): no own heading, buttons stay at the bottom while rows scroll. */
  sheet?: boolean
}

function Row({ c }: { c: Change }) {
  const risky = c.risks.length > 0
  return (
    <tr data-risk={risky || undefined}>
      <th scope="row">
        <span className={styles.rowName}>
          {risky && <AlertTriangle size={14} aria-hidden />}
          {rowLabel(c)}
        </span>
      </th>
      <td>
        {formatChange(c)}
        {c.risks.map((r) => (
          <span key={r} className={styles.rowRisk}>
            {riskText(r)}
          </span>
        ))}
      </td>
    </tr>
  )
}

function Item({ item, name, several, first }: { item: SensorPreview; name: string; several: boolean; first: boolean }) {
  const body = item.error ? (
    <p className={styles.itemError}>{t.diff.itemError(item.error)}</p>
  ) : item.changes.length === 0 ? (
    <p className={styles.note}>{t.diff.noChange}</p>
  ) : (
    <table className={styles.rowsTable}>
      <caption className="visually-hidden">{t.diff.caption(name)}</caption>
      <thead>
        <tr>
          <th scope="col">{t.diff.colItem}</th>
          <th scope="col">{t.diff.colChange}</th>
        </tr>
      </thead>
      <tbody>
        {item.changes.map((c, i) => (
          <Row key={i} c={c} />
        ))}
      </tbody>
    </table>
  )
  const head = (
    <>
      {several && <ChevronRight size={16} className={styles.chevron} aria-hidden />}
      {item.error && <XCircle size={16} className={styles.errIcon} aria-hidden />}
      <span>{name}</span>
      <span className={styles.cardCount}>{t.diff.count(item.changes.length)}</span>
      {item.risks.map((r) => (
        <span key={r} className={styles.riskChip} title={riskText(r)}>
          <AlertTriangle size={12} aria-hidden />
          <span className="visually-hidden">{riskText(r)}</span>
        </span>
      ))}
    </>
  )
  if (!several) {
    return (
      <section className={styles.card} data-error={Boolean(item.error)} aria-label={name}>
        <div className={styles.cardHead}>{head}</div>
        {body}
      </section>
    )
  }
  // the first card, and every risky or failed one, start open
  return (
    <details
      className={styles.card}
      data-error={Boolean(item.error)}
      open={first || item.risks.length > 0 || Boolean(item.error)}
    >
      <summary className={styles.cardHead}>{head}</summary>
      {body}
    </details>
  )
}

/**
 * The server's diff (G24): per sensor before → after, risky rows highlighted (G29). Nothing is written until
 * the primary is pressed; it stays disabled while a sensor cannot be applied, nothing changes, or an
 * absolute overwrite is not acknowledged.
 */
export function DiffPreview(p: Props) {
  const { preview } = p
  const ackId = useId()
  const titleId = useId()
  const [ack, setAck] = useState(Boolean(p.ackInitially))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const several = preview.items.length > 1
  const rows = preview.items.reduce((n, i) => n + i.changes.length, 0)
  const risky = preview.items.reduce((n, i) => n + i.changes.filter((c) => c.risks.length > 0).length, 0)
  const errored = preview.items.filter((i) => i.error !== null).map((i) => i.device_id)
  const nothing = errored.length === 0 && rows === 0
  const needAck = needsOverwriteAck(preview)
  const blocked = errored.length > 0 || nothing || (needAck && !ack)

  const apply = async () => {
    setBusy(true)
    setError(null)
    try {
      await p.onApply()
    } catch (e) {
      setError(failureText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={styles.diff} aria-labelledby={p.sheet ? undefined : titleId}>
      {!p.sheet && (
        <h2 id={titleId} className={cal.headline}>
          {p.title ?? t.diff.title}
        </h2>
      )}
      <p className={styles.diffSummary}>{t.diff.summary(preview.items.length, rows, risky)}</p>
      {preview.risks.length > 0 && (
        <ul className={styles.riskChips} aria-label={t.diff.title}>
          {preview.risks.map((r) => (
            <li key={r} className={styles.riskChip}>
              <AlertTriangle size={14} aria-hidden />
              {riskText(r)}
            </li>
          ))}
        </ul>
      )}
      {preview.items.map((item, i) => (
        <Item
          key={item.device_id}
          item={item}
          name={p.names[item.device_id] ?? item.device_id}
          several={several}
          first={i === 0}
        />
      ))}

      <div className={`${styles.diffActions} ${p.sheet ? styles.sheetActions : ''}`}>
        {needAck && (
          <label className={cal.override} htmlFor={ackId}>
            <input id={ackId} type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            {t.diff.ackOverwrite}
          </label>
        )}
        {errored.length > 0 && (
          <div className={styles.banner} role="alert">
            <span>
              <XCircle size={16} aria-hidden />
              {t.diff.hasErrors}
            </span>
            {p.onDropErrors && errored.length < preview.items.length && (
              <Button icon={Filter} disabled={p.dropping} onClick={() => p.onDropErrors?.(errored)}>
                {t.diff.dropErrors}
              </Button>
            )}
          </div>
        )}
        {nothing && <p className={styles.reason}>{t.diff.nothing}</p>}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        <Button variant="primary" size="lg" block icon={Upload} disabled={blocked || busy} onClick={apply}>
          {p.applyLabel}
        </Button>
        <Button variant="ghost" icon={ArrowLeft} onClick={p.onBack} disabled={busy}>
          {t.diff.back}
        </Button>
      </div>
    </section>
  )
}
