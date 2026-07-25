/**
 * The verbatim WhatsApp messages an event was extracted from.
 *
 * This carries nearly all the trust value in the product: an LLM wrote the card above it, and the
 * only reason a family believes "Mum missed her evening apixaban" is that they can open this and
 * read the sentence Tom actually typed, with his name and the exact minute he sent it. So it shows
 * the quote unedited, never truncated, and the time to the minute even when the event itself is
 * only placed to the month — the message timestamp is a fact, the event date is an inference.
 *
 * Collapsed by default: evidence you have to ask for reads as available, not as clutter.
 */

import { formatDate, formatTime } from '../lib/datetime'
import type { SourceExcerpt } from '../types/api'
import styles from './source-disclosure.module.css'

export interface SourceDisclosureProps {
  excerpts: SourceExcerpt[]
  timeZone: string
}

export function SourceDisclosure({ excerpts, timeZone }: SourceDisclosureProps) {
  if (excerpts.length === 0) {
    return (
      <p className={styles.authored}>Added by hand — there is no chat message behind this one.</p>
    )
  }

  return (
    <details className={styles.disclosure}>
      <summary className={styles.summary}>
        <Chevron />
        {excerpts.length === 1
          ? 'Read the message this came from'
          : `Read the ${excerpts.length} messages this came from`}
      </summary>
      <ul className={styles.quotes}>
        {excerpts.map((excerpt) => (
          <li key={excerpt.message_id}>
            <figure className={styles.figure}>
              <blockquote className={styles.quote}>{excerpt.quote}</blockquote>
              <figcaption className={styles.attribution}>
                <span className={styles.sender}>{excerpt.sender}</span>
                <span className={styles.sep} aria-hidden="true">
                  ·
                </span>
                <time dateTime={excerpt.sent_at}>
                  {formatDate(excerpt.sent_at, timeZone)} at {formatTime(excerpt.sent_at, timeZone)}
                </time>
              </figcaption>
            </figure>
          </li>
        ))}
      </ul>
    </details>
  )
}

function Chevron() {
  return (
    <svg
      className={styles.chevron}
      width="10"
      height="10"
      viewBox="0 0 10 10"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3 1 L7 5 L3 9" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}
