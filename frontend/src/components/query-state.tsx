/**
 * What the feed shows while a query is in flight, and when one fails.
 *
 * The retry button is offered for 5xx and transport failures only. A 4xx is a settled answer —
 * retrying a 422 produces the identical 422, and retrying a 401 just delays the login redirect
 * behind another spinner — so the button is absent rather than present and useless. Same rule the
 * query client enforces automatically, made visible here.
 */

import { ApiError } from '../api/client'
import styles from './states.module.css'

export interface ErrorStateProps {
  error: unknown
  title?: string
  onRetry?: () => void
}

export function ErrorState({ error, title = 'That did not load', onRetry }: ErrorStateProps) {
  const apiError = error instanceof ApiError ? error : null
  const retryable = apiError === null || apiError.status >= 500
  const detail =
    error instanceof Error ? error.message : 'Something went wrong. Try again in a moment.'

  return (
    <div className={styles.error} role="alert">
      <h3 className={styles.errorTitle}>{title}</h3>
      <span className={styles.errorDetail}>{detail}</span>
      {retryable && onRetry ? (
        <button type="button" className={styles.retry} onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

const SKELETON_ROWS = [1, 2, 3]

/** Card-shaped placeholders, so the feed does not jump when the real cards land. */
export function FeedSkeleton({ label = 'Loading the feed' }: { label?: string }) {
  return (
    <div className={styles.skeleton} aria-busy="true">
      <span className={styles.srOnly}>{label}</span>
      {SKELETON_ROWS.map((row) => (
        <div key={row} className={styles.skeletonCard} aria-hidden="true" />
      ))}
    </div>
  )
}
