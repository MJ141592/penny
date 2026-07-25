/**
 * Compile-time proof that the hand-authored fixtures still match `docs/api-contract.md`.
 *
 * WHY THIS TYPE EXISTS. `import feed from './feed.json'` widens every string literal to
 * `string`, so `feed.events` is never assignable to `Event` however correct it is — which is
 * why the fixtures were cast through `unknown`. A double cast silences the compiler
 * completely: a fixture missing `status`, or spelling `symptom_name` instead of `symptom`,
 * would compile and then render a blank card at runtime with nothing to point at.
 *
 * `Loosen<T>` relaxes exactly the one thing JSON import loses — literal-union narrowing — and
 * nothing else. Missing fields, misspelled fields, `null` where a string is required and the
 * wrong `details` shape for a `kind` are all still compile errors, because the four `details`
 * shapes stay structurally distinct after loosening.
 *
 * When M4 replaces these fixtures with `openapi-typescript` output, delete this file.
 */

export type Loosen<T> = T extends string
  ? string
  : T extends readonly (infer U)[]
    ? Loosen<U>[]
    : T extends object
      ? { [K in keyof T]: Loosen<T[K]> }
      : T
