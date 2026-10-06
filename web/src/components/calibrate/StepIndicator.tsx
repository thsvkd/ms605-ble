import { useStrings } from '../../strings'
import styles from './calibrate.module.css'

export type CalibStep = 0 | 1 | 2 | 3

/** 1 센서 선택 · 2 사전점검 · 3 진행 · 4 결과 — where the operator is, and that nothing is skipped. */
export function StepIndicator({ current }: { current: CalibStep }) {
  const t = useStrings()
  return (
    <ol className={styles.steps} aria-label={t.calib.stepsLabel}>
      {t.calib.steps.map((label, i) => (
        <li
          key={label}
          className={styles.step}
          data-state={i < current ? 'done' : i === current ? 'current' : 'todo'}
          aria-current={i === current ? 'step' : undefined}
        >
          {label}
        </li>
      ))}
    </ol>
  )
}
