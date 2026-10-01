import { ChevronRight, FileUp } from 'lucide-react'
import { useId, useState } from 'react'
import { ApiRequestError, importSensorInfo } from '../api/client'
import { errorText, t } from '../strings'
import { Button } from './Button'
import { type SiteChoice, SitePicker } from './SitePicker'
import styles from './gather.module.css'

/** Collapsed secondary area: import a sensor_info_*.yaml as pending entries (6.5). */
export function ImportSensorInfo() {
  const uid = useId()
  const [file, setFile] = useState<File | null>(null)
  const [site, setSite] = useState<SiteChoice>({ kind: 'auto' })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)

  const submit = async () => {
    if (!file) return
    if (site.kind === 'new' && !site.name.trim()) {
      setResult({ ok: false, text: t.form.required })
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
      setResult({ ok: true, text: n > 0 ? t.import.done(n) : t.import.none })
    } catch (e) {
      setResult({ ok: false, text: e instanceof ApiRequestError ? errorText(e.code, e.message) : t.error.internal })
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
          <p className={`${styles.result} ${result.ok ? '' : styles.resultError}`} role="status">
            {result.text}
          </p>
        )}
      </form>
    </details>
  )
}
