import { ChevronRight, FileUp } from 'lucide-react'
import { useId, useState } from 'react'
import { ApiRequestError, importSensorInfo } from '../api/client'
import type { ErrorCode } from '../api/types'
import { errorText, useStrings } from '../strings'
import { Button } from './Button'
import { type SiteChoice, SitePicker } from './SitePicker'
import styles from './gather.module.css'

type Result =
  | { kind: 'required' }
  | { kind: 'done'; count: number }
  | { kind: 'error'; code?: ErrorCode; message?: string }

/** Collapsed secondary area: import a sensor_info_*.yaml as pending entries (6.5). */
export function ImportSensorInfo() {
  const t = useStrings()
  const uid = useId()
  const [file, setFile] = useState<File | null>(null)
  const [site, setSite] = useState<SiteChoice>({ kind: 'auto' })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null)

  const submit = async () => {
    if (!file) return
    if (site.kind === 'new' && !site.name.trim()) {
      setResult({ kind: 'required' })
      return
    }
    setBusy(true)
    setResult(null)
    try {
      const content = await file.text()
      const res = await importSensorInfo({
        filename: file.name,
        content,
        site_id: site.kind === 'existing' ? site.siteId : null,
        site_name: site.kind === 'new' ? site.name.trim() : null,
      })
      const n = res.added.length
      setResult({ kind: 'done', count: n })
    } catch (e) {
      setResult(e instanceof ApiRequestError ? { kind: 'error', code: e.code, message: e.message } : { kind: 'error' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className={styles.details}>
      <summary className={styles.summary}>
        <ChevronRight size={18} className={styles.chevron} aria-hidden />
        {t.import.title}
      </summary>
      <form
        className={styles.detailsBody}
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <label htmlFor={`${uid}-file`} className="visually-hidden">
          {t.import.file}
        </label>
        <input
          id={`${uid}-file`}
          type="file"
          accept=".yaml,.yml,.txt"
          className={styles.fileInput}
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null)
            setResult(null)
          }}
        />
        <SitePicker id={`${uid}-site`} value={site} onChange={setSite} autoLabel={t.import.siteAuto} />
        <Button type="submit" icon={FileUp} disabled={!file || busy}>
          {t.import.action}
        </Button>
        {result && (
          <p className={`${styles.result} ${result.kind === 'done' ? '' : styles.resultError}`} role="status">
            {result.kind === 'required'
              ? t.form.required
              : result.kind === 'done'
                ? result.count > 0
                  ? t.import.done(result.count)
                  : t.import.none
                : result.code
                  ? errorText(result.code, result.message ?? '')
                  : t.error.internal}
          </p>
        )}
      </form>
    </details>
  )
}
