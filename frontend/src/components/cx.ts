/**
 * Joins class names, dropping the falsy ones.
 *
 * CSS Module lookups are `string | undefined` under `noUncheckedIndexedAccess`, so every template
 * literal that composes two of them needs a `?? ''` somewhere. This is that somewhere.
 */
export function cx(...parts: (string | undefined | false)[]): string {
  return parts.filter(Boolean).join(' ')
}
