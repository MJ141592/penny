/**
 * The three ways this feed can legitimately have nothing in it, told apart.
 *
 * "No events" and "no messages" look identical on screen and are completely different problems:
 * one family has imported a chat and Penny found nothing worth recording, the other has not
 * imported anything yet. Only the second is fixable by the reader, and only it should send them
 * to the import flow — a "get started" call to action shown to someone who already did is the
 * fastest way to make a product feel like it isn't listening.
 */

import type { ReactNode } from 'react'
import { Link } from 'react-router'

import { cx } from './cx'
import styles from './states.module.css'

export interface EmptyStateProps {
  title: string
  children: ReactNode
  action?: ReactNode
  /** Quiet variant for panels that already have a border of their own. */
  inline?: boolean
}

export function EmptyState({ title, children, action, inline }: EmptyStateProps) {
  return (
    <div className={cx(styles.empty, inline && styles.inline)}>
      <h3 className={styles.emptyTitle}>{title}</h3>
      <p className={styles.emptyBody}>{children}</p>
      {action ? <p className={styles.emptyAction}>{action}</p> : null}
    </div>
  )
}

/**
 * `messageCount` is what makes this two states rather than one. `undefined` while the session is
 * still loading, in which case we say the neutral thing.
 */
export function FeedEmptyState({ messageCount }: { messageCount?: number | undefined }) {
  if (messageCount === 0) {
    return (
      <EmptyState
        title="No messages imported yet"
        action={<Link to="/import">Import a WhatsApp export</Link>}
      >
        Penny reads the family's WhatsApp conversation and turns it into a history you can browse.
        Export the group chat from WhatsApp and upload the <code>.txt</code> file to get started.
      </EmptyState>
    )
  }

  return (
    <EmptyState title="Nothing recorded yet" action={<Link to="/import">Import more history</Link>}>
      The messages are in, but Penny hasn't found any appointments, symptoms or medication changes
      in them yet. Newly imported messages can take a few minutes to come through.
    </EmptyState>
  )
}

export function UpcomingEmptyState() {
  return (
    <EmptyState title="Nothing scheduled" inline>
      Appointments the family mentions in the chat will appear here as soon as they are booked.
    </EmptyState>
  )
}
