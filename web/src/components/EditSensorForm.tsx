import { useEffect, useId, useState } from 'react'
import { ApiRequestError, ensureSite, updateSensor } from '../api/client'
import type { ErrorCode, RegistryInfo, SensorUpdate } from '../api/types'
import { errorText, useStrings } from '../strings'
import { Button } from './Button'
import { Field } from './Field'
import { type SiteChoice, SitePicker } from './SitePicker'
import styles from './detail.module.css'

/** `edit` once it means the same as the server's `value` again (after a save, or another screen's same change). */
function settle(edit: string | null, value: string, trim = true): string | null {
  return edit !== null && (trim ? edit.trim() : edit) === value ? null : edit
}

/**
 * Alias, site, location and notes of a registered sensor (PATCH, 6.3). A field the user has not touched
 * (`null` edit) shows the server's current value, follows other screens' changes and is not sent:
 * saving one field never overwrites another screen's change to a different one.
 */
export function EditSensorForm({ deviceId, registry }: { deviceId: string; registry: RegistryInfo }) {
  const t = useStrings()
  const uid = useId()
  const [aliasEdit, setAliasEdit] = useState<string | null>(null)
  const [siteEdit, setSiteEdit] = useState<SiteChoice | null>(null)
  const [locationEdit, setLocationEdit] = useState<string | null>(null)
  const [notesEdit, setNotesEdit] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [aliasError, setAliasError] = useState(false)
  const [siteError, setSiteError] = useState(false)
  const [result, setResult] = useState<{ ok: true } | { ok: false; code?: ErrorCode; message?: string } | null>(null)

  const alias = aliasEdit ?? registry.alias
  const site: SiteChoice = siteEdit ?? { kind: 'existing', siteId: registry.site_id }
  const location = locationEdit ?? registry.location
  const notes = notesEdit ?? registry.notes

  useEffect(() => {
    setAliasEdit((e) => settle(e, registry.alias))
    setSiteEdit((e) => (e?.kind === 'existing' && e.siteId === registry.site_id ? null : e))
    setLocationEdit((e) => settle(e, registry.location))
    setNotesEdit((e) => settle(e, registry.notes, false))
  }, [registry.alias, registry.site_id, registry.location, registry.notes])

  const save = async () => {
    const name = alias.trim()
    const newSite = site.kind === 'new' ? site.name.trim() : null
    setAliasError(!name)
    setSiteError(newSite === '')
    setResult(null)
    if (!name || newSite === '') return
    setBusy(true)
    try {
      const body: Partial<SensorUpdate> = {}
      if (aliasEdit !== null && name !== registry.alias) body.alias = name
      if (locationEdit !== null && location.trim() !== registry.location) body.location = location.trim()
      if (notesEdit !== null && notes !== registry.notes) body.notes = notes
      if (newSite !== null) {
        const siteId = await ensureSite(newSite)
        setSiteEdit({ kind: 'existing', siteId }) // a retry after a failed PATCH reuses it
        body.site_id = siteId
      } else if (site.kind === 'existing' && site.siteId !== registry.site_id) {
        body.site_id = site.siteId
      }
      await updateSensor(deviceId, body)
      setResult({ ok: true })
    } catch (e) {
      setResult(e instanceof ApiRequestError ? { ok: false, code: e.code, message: e.message } : { ok: false })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className={styles.card}
      aria-labelledby={`${uid}-title`}
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <h2 id={`${uid}-title`} className={styles.cardTitle}>
        {t.detail.edit}
      </h2>
      <div className={styles.fields}>
        <Field id={`${uid}-alias`} label={t.form.alias} error={aliasError ? t.form.required : null}>
          {(p) => (
            <input {...p} value={alias} maxLength={64} autoComplete="off" onChange={(e) => setAliasEdit(e.target.value)} />
          )}
        </Field>
        <SitePicker
          id={`${uid}-site`}
          value={site}
          onChange={setSiteEdit}
          nameError={siteError ? t.form.required : null}
        />
        <div className={styles.wide}>
          <Field id={`${uid}-location`} label={t.form.location}>
            {(p) => (
              <input
                {...p}
                value={location}
                maxLength={200}
                placeholder={t.form.locationPlaceholder}
                autoComplete="off"
                onChange={(e) => setLocationEdit(e.target.value)}
              />
            )}
          </Field>
        </div>
        <div className={styles.wide}>
          <Field id={`${uid}-notes`} label={t.form.notes}>
            {(p) => <textarea {...p} value={notes} maxLength={2000} onChange={(e) => setNotesEdit(e.target.value)} />}
          </Field>
        </div>
      </div>
      <div className={styles.actions}>
        {result && (
          <p className={result.ok ? styles.saved : styles.error} role="status">
            {result.ok ? t.form.saved : result.code ? errorText(result.code, result.message ?? '') : t.error.internal}
          </p>
        )}
        <Button type="submit" variant="primary" disabled={busy}>
          {t.form.save}
        </Button>
      </div>
    </form>
  )
}
