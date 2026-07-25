import { useState } from 'react'

import { useMembers, useSession, useUpdateHousehold } from '../api/queries'
import { formatDate } from '../lib/datetime'
import styles from './routes.module.css'

export function SettingsRoute() {
  const session = useSession()
  const members = useMembers()
  const update = useUpdateHousehold()
  const household = session.data?.household

  const [draft, setDraft] = useState<{ name: string; care_recipient_name: string; timezone: string } | null>(
    null,
  )
  // Seed the form from the session the first time it lands, then let the user own it.
  const form = draft ?? {
    name: household?.name ?? '',
    care_recipient_name: household?.care_recipient_name ?? '',
    timezone: household?.timezone ?? 'Europe/London',
  }

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Settings</h1>
        {session.data ? (
          <p className={styles.hint}>
            {session.data.counts.events} events from {session.data.counts.messages} messages.
          </p>
        ) : null}
      </div>

      <section className={styles.section}>
        <h2>Household</h2>
        {update.error ? <p className={styles.error}>{update.error.message}</p> : null}
        <form
          className={styles.form}
          onSubmit={(submit) => {
            submit.preventDefault()
            update.mutate(form)
          }}
        >
          <div className={styles.field}>
            <label className={styles.label} htmlFor="household-name">
              Family name
            </label>
            <input
              id="household-name"
              className={styles.input}
              value={form.name}
              onChange={(change) => setDraft({ ...form, name: change.target.value })}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="care-recipient">
              Who is being cared for
            </label>
            <input
              id="care-recipient"
              className={styles.input}
              value={form.care_recipient_name}
              onChange={(change) => setDraft({ ...form, care_recipient_name: change.target.value })}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="timezone">
              Timezone
            </label>
            <input
              id="timezone"
              className={styles.input}
              value={form.timezone}
              onChange={(change) => setDraft({ ...form, timezone: change.target.value })}
            />
            <span className={styles.hint}>
              An IANA name. Every date in the feed is rendered in this zone, not the browser's.
            </span>
          </div>

          <div className={styles.actions}>
            <button
              type="submit"
              className={`${styles.button} ${styles.buttonPrimary}`}
              disabled={update.isPending}
            >
              {update.isPending ? 'Saving…' : 'Save'}
            </button>
            {update.isSuccess && !draft ? <span className={styles.hint}>Saved</span> : null}
          </div>
        </form>
      </section>

      <section className={styles.section}>
        <h2>People in the chat</h2>
        <p className={styles.hint}>
          A family that imports history and also pairs WhatsApp will see the same person twice —
          exports carry a name with no phone id. Merging them is a later job.
        </p>
        {members.isPending ? <p className={styles.hint}>Loading…</p> : null}
        {members.data ? (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Messages</th>
                <th>WhatsApp id</th>
                <th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {members.data.map((member) => (
                <tr key={member.id}>
                  <td>{member.display_name}</td>
                  <td>{member.message_count}</td>
                  <td>{member.wa_jid ?? member.wa_lid ?? 'from an export'}</td>
                  <td>{formatDate(member.last_seen_at, form.timezone)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </>
  )
}
