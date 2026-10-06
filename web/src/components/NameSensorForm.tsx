import { useEffect, useId, useRef, useState } from 'react'
import { ApiRequestError, createSensor, ensureSite, failureText } from '../api/client'
import { defaultAlias, readLastSite, writeLastSite } from '../alias'
import { type Store, useStore } from '../store/store'
import { useStrings } from '../strings'
import { Button } from './Button'
import { Field } from './Field'
import { type SiteChoice, SitePicker } from './SitePicker'
import styles from './gather.module.css'

export type NameResult = 'saved' | 'taken'

interface Props {
  deviceId: string
  bleName: string | null
  /** Only when the user explicitly asked to name it (no surprise phone keyboard otherwise). */
  autoFocus?: boolean
  onDone: (result?: NameResult) => void
}

/** Site default: the last used one if it still exists, else the only site, else "새 사이트…" (9.4). */
export function initialSite(state: Pick<Store, 'sites'>): SiteChoice {
  const last = readLastSite()
  if (last !== null && last in state.sites) return { kind: 'existing', siteId: last }
  const ids = Object.keys(state.sites)
  if (ids.length === 1 && ids[0] !== undefined) return { kind: 'existing', siteId: ids[0] }
  return { kind: 'new', name: '' }
}

export function NameSensorForm({ deviceId, bleName, autoFocus, onDone }: Props) {
  const t = useStrings()
  const uid = useId()
  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const [site, setSite] = useState<SiteChoice>(() => initialSite(useStore.getState()))
  const siteTouched = useRef(false)
  const siteId = site.kind === 'existing' ? site.siteId : null
  const [aliasEdit, setAliasEdit] = useState<string | null>(null) // null: follow the default
  const alias = aliasEdit ?? defaultAlias(sensors, siteId, sensors[deviceId]?.live)
  const [location, setLocation] = useState('')
  const [saving, setSaving] = useState(false)
  const [aliasError, setAliasError] = useState(false)
  const [siteError, setSiteError] = useState(false)
  const [formError, setFormError] = useState<unknown>(null)
  const aliasRef = useRef<HTMLInputElement>(null)
  const siteNameRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!autoFocus) return
    if (site.kind === 'new') siteNameRef.current?.focus()
    else aliasRef.current?.focus()
  }, [autoFocus])

  useEffect(() => {
    if (siteTouched.current || site.kind !== 'new' || site.name !== '') return
    const next = initialSite({ sites })
    if (next.kind === 'existing') setSite(next)
  }, [site, sites])

  const save = async () => {
    const name = alias.trim()
    const newSite = site.kind === 'new' ? site.name.trim() : null
    setAliasError(!name)
    setSiteError(newSite === '')
    setFormError(null)
    if (!name || newSite === '') return
    setSaving(true)
    try {
      let target = siteId
      if (newSite !== null) {
        target = await ensureSite(newSite)
        setSite({ kind: 'existing', siteId: target }) // a retry after a failed createSensor reuses it
      }
      if (target === null) return
      const savedAlias = aliasEdit ?? defaultAlias(sensors, target, sensors[deviceId]?.live)
      await createSensor({ device_id: deviceId, site_id: target, alias: savedAlias.trim(), location: location.trim(), notes: '' })
      writeLastSite(target)
      onDone('saved')
    } catch (e) {
      if (e instanceof ApiRequestError && e.code === 'already_exists') {
        onDone('taken')
        return
      }
      setFormError(e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      className={styles.nameForm}
      aria-label={bleName ? `${t.form.nameAction}: ${bleName}` : t.form.nameAction}
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <div className={styles.formFields}>
        <Field id={`${uid}-alias`} label={t.form.alias} error={aliasError ? t.form.required : null}>
          {(p) => (
            <input
              {...p}
              ref={aliasRef}
              value={alias}
              maxLength={64}
              autoComplete="off"
              enterKeyHint="done"
              onChange={(e) => setAliasEdit(e.target.value)}
            />
          )}
        </Field>
        <SitePicker
          id={`${uid}-site`}
          value={site}
          onChange={(next) => {
            siteTouched.current = true
            setSite(next)
          }}
          nameError={siteError ? t.form.required : null}
          nameInputRef={siteNameRef}
        />
        <Field id={`${uid}-location`} label={t.form.location}>
          {(p) => (
            <input
              {...p}
              value={location}
              maxLength={200}
              placeholder={t.form.locationPlaceholder}
              autoComplete="off"
              enterKeyHint="done"
              onChange={(e) => setLocation(e.target.value)}
            />
          )}
        </Field>
      </div>
      {formError !== null && (
        <p className={styles.formError} role="alert">
          {failureText(formError)}
        </p>
      )}
      <div className={styles.formActions}>
        <Button onClick={() => onDone()}>{t.form.later}</Button>
        <Button type="submit" variant="primary" disabled={saving} className={styles.saveButton}>
          {t.form.save}
        </Button>
      </div>
    </form>
  )
}
