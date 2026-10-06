import { useSyncExternalStore } from 'react'
import type { ErrorCode } from './api/types'
import { en } from './locales/en'
import { ko, type Strings } from './locales/ko'

export type Locale = 'ko' | 'en'
const STORAGE_KEY = 'ms605.language'
const catalogs: Record<Locale, Strings> = { ko, en }
let locale: Locale = 'ko'
/** Live binding for non-React formatters and event handlers. Components use useStrings. */
export let t: Strings = ko
const listeners = new Set<() => void>()
const subscribe = (listener: () => void) => {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
export const getLocale = (): Locale => locale

/** Saved choice wins; otherwise use the first supported browser language, then English. */
export function readLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'ko' || saved === 'en') return saved
  } catch {
    // Private browsing can disable storage; language selection still works in this tab.
  }
  for (const language of navigator.languages?.length ? navigator.languages : [navigator.language]) {
    const base = language.toLowerCase().split('-')[0]
    if (base === 'ko' || base === 'en') return base
  }
  return 'en'
}

function applyLocale(next: Locale): void {
  document.documentElement.lang = next
  if (locale === next) return
  locale = next
  t = catalogs[next]
  for (const listener of listeners) listener()
}

/** Call before rendering or opening connections, without saving an implicit browser preference. */
export function initializeLocale(): void {
  applyLocale(readLocale())
}

export function setLocale(next: Locale): void {
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // Keep the selection for this page even if browser storage is unavailable.
  }
  applyLocale(next)
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribe, getLocale)
}

export function useStrings(): Strings {
  return catalogs[useLocale()]
}

export function errorText(code: ErrorCode, message: string): string {
  if (code === 'invalid_file') return t.error.invalid_file(message)
  return t.error[code]
}
