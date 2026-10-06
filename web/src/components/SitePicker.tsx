import { type Ref, useMemo } from 'react'
import { selectSites, useStore } from '../store/store'
import { t } from '../strings'
import { Field } from './Field'

export type SiteChoice = { kind: 'existing'; siteId: string } | { kind: 'new'; name: string } | { kind: 'auto' }

const NEW = '__new__'
const AUTO = '__auto__'

interface Props {
  id: string
  value: SiteChoice
  onChange: (value: SiteChoice) => void
  /** Offer "derive from the file name" (yaml import). */
  autoLabel?: string
  nameError?: string | null
  nameInputRef?: Ref<HTMLInputElement>
}

/** Existing sites plus a trailing "새 사이트…" that reveals a name field (9.4). */
export function SitePicker({ id, value, onChange, autoLabel, nameError, nameInputRef }: Props) {
  const siteMap = useStore((s) => s.sites)
  const sites = useMemo(() => selectSites({ sites: siteMap }), [siteMap])
  const selected = value.kind === 'existing' ? value.siteId : value.kind === 'new' ? NEW : AUTO
  return (
    <>
      <Field id={id} label={t.form.site}>
        {(p) => (
          <select
            {...p}
            value={selected}
            onChange={(e) => {
              const v = e.target.value
              if (v === NEW) onChange({ kind: 'new', name: '' })
              else if (v === AUTO) onChange({ kind: 'auto' })
              else onChange({ kind: 'existing', siteId: v })
            }}
          >
            {autoLabel && <option value={AUTO}>{autoLabel}</option>}
            {sites.map((s) => (
              <option key={s.site_id} value={s.site_id}>
                {s.name}
              </option>
            ))}
            <option value={NEW}>{t.form.newSite}</option>
          </select>
        )}
      </Field>
      {value.kind === 'new' && (
        <Field id={`${id}-name`} label={t.form.newSiteName} error={nameError}>
          {(p) => (
            <input
              {...p}
              ref={nameInputRef}
              value={value.name}
              maxLength={64}
              autoComplete="off"
              enterKeyHint="done"
              onChange={(e) => onChange({ kind: 'new', name: e.target.value })}
            />
          )}
        </Field>
      )}
    </>
  )
}
