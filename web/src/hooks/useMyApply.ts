import type { ApplyJobView } from '../api/types'
import { useDrafts } from '../store/drafts'
import { useStore } from '../store/store'

/** The server's job when this client started it from `scope` and has not dismissed it (G25: the server keeps one). */
export function useMyApply(scope: string): {
  job: ApplyJobView | null
  start: (job: ApplyJobView) => void
  dismiss: () => void
} {
  const apply = useStore((s) => s.apply)
  const mine = useDrafts((s) => s.mine[scope])
  const setMine = useDrafts((s) => s.setMine)
  return {
    job: apply !== null && mine?.applyId === apply.apply_id ? apply : null,
    start: (job) => setMine(scope, { applyId: job.apply_id, settled: false }),
    dismiss: () => setMine(scope, null),
  }
}
