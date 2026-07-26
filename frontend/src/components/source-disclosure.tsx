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
 *
 * A SPOKEN QUOTE SAYS SO. `transcribed` means nobody typed those words: a model listened to a
 * voice note and wrote down what it thought it heard. That is a second inference stacked under
 * the first, and it fails in a specific, dangerous way — speech-to-text mishears proper nouns,
 * so "Apixaban" and "a pixie band" are the same sound and only one of them is a blood thinner.
 * Rendering it identically to a typed message would present a guess as the underlying fact the
 * whole panel exists to expose, and would give the one person who could catch it — someone who
 * knows the recording is still in the chat, and knows what Mum actually says — no reason to
 * check. So: labelled per quote, and one plain sentence at the bottom saying what to do about it.
 */

import { formatDate, formatTime } from '../lib/datetime'
import type { SourceExcerpt } from '../types/api'
import { cx } from './cx'
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

  const spoken = excerpts.filter((excerpt) => excerpt.transcribed).length

  return (
    <details className={styles.disclosure}>
      <summary className={styles.summary}>
        <Chevron />
        {summaryLabel(excerpts.length, spoken)}
      </summary>
      <ul className={styles.quotes}>
        {excerpts.map((excerpt) => (
          <li key={excerpt.message_id}>
            <figure className={cx(styles.figure, excerpt.transcribed && styles.figureSpoken)}>
              <blockquote className={styles.quote}>{excerpt.quote}</blockquote>
              <figcaption className={styles.attribution}>
                <span className={styles.sender}>{excerpt.sender}</span>
                <span className={styles.sep} aria-hidden="true">
                  ·
                </span>
                <time dateTime={excerpt.sent_at}>
                  {formatDate(excerpt.sent_at, timeZone)} at {formatTime(excerpt.sent_at, timeZone)}
                </time>
                {excerpt.transcribed ? (
                  <>
                    <span className={styles.sep} aria-hidden="true">
                      ·
                    </span>
                    <span className={styles.spoken}>
                      <Microphone />
                      voice note
                    </span>
                  </>
                ) : null}
              </figcaption>
            </figure>
          </li>
        ))}
      </ul>
      {spoken > 0 ? (
        <p className={styles.spokenNote}>
          {spoken === 1 ? 'One of these was spoken' : `${spoken} of these were spoken`}, not typed,
          and written down automatically. Names, medicines and doses are the words that get
          misheard — the original recording is still in the WhatsApp chat if something reads
          oddly.
        </p>
      ) : null}
    </details>
  )
}

/**
 * What the collapsed row says. The voice note is named here, not only inside, because the panel
 * is shut by default — a family scrolling the feed should be able to see that the evidence for a
 * card is a recording without opening every card to find out.
 */
function summaryLabel(total: number, spoken: number): string {
  if (total === 1) {
    return spoken === 1 ? 'Read the voice note this came from' : 'Read the message this came from'
  }
  if (spoken === 0) {
    return `Read the ${total} messages this came from`
  }
  if (spoken === total) {
    return `Read the ${total} voice notes this came from`
  }
  return `Read the ${total} messages this came from, ${spoken} of them spoken`
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

/** Decorative. "voice note" sits next to it in text, so nothing is carried by the glyph alone. */
function Microphone() {
  return (
    <svg
      className={styles.mic}
      width="11"
      height="11"
      viewBox="0 0 12 12"
      aria-hidden="true"
      focusable="false"
    >
      <rect
        x="4.4"
        y="1"
        width="3.2"
        height="6"
        rx="1.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.1"
      />
      <path
        d="M2.6 5.6a3.4 3.4 0 0 0 6.8 0M6 9v2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinecap="round"
      />
    </svg>
  )
}
