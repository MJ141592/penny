/**
 * Everything about the household that isn't its history: who it is for, what timezone its dates
 * mean, the shared password, the WhatsApp link, and who Penny thinks is in the chat.
 *
 * Four panels rather than one long form, because they fail independently — GOWA being unreachable
 * must not stop somebody renaming the household — and because the WhatsApp one is the only thing
 * on the screen that polls.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import {
  useChangePassword,
  useLinkWhatsappGroup,
  useMembers,
  useMergeMember,
  useSession,
  useUpdateHousehold,
  useWhatsappRelink,
  useWhatsappStatus,
} from '../api/queries'
import { ErrorState } from '../components'
import { formatDate } from '../lib/datetime'
import type { Member, WhatsappStatus } from '../types/api'
import { TimezoneField } from './timezone-field'
import styles from './routes.module.css'

export function SettingsRoute() {
  const session = useSession()
  const household = session.data?.household

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Settings</h1>
        {session.data ? (
          <p className={styles.hint}>
            {session.data.counts.events.toLocaleString('en-GB')} entries read from{' '}
            {session.data.counts.messages.toLocaleString('en-GB')} messages.
          </p>
        ) : null}
      </div>

      <HouseholdPanel
        key={household?.id ?? 'loading'}
        name={household?.name ?? ''}
        careRecipient={household?.care_recipient_name ?? ''}
        timezone={household?.timezone ?? 'Europe/London'}
      />
      <PasswordPanel />
      <WhatsappPanel />
      <MembersPanel timezone={household?.timezone ?? 'Europe/London'} />
    </>
  )
}

/* ------------------------------------------------------------------------------ household ---- */

/**
 * Keyed on the household id so the draft state is thrown away if the session ever changes
 * underneath it — otherwise the previous family's name would sit in the box after a sign-out and
 * sign-in on the same laptop.
 */
function HouseholdPanel({
  name,
  careRecipient,
  timezone,
}: {
  name: string
  careRecipient: string
  timezone: string
}) {
  const update = useUpdateHousehold()
  const [form, setForm] = useState({ name, care_recipient_name: careRecipient, timezone })
  const dirty =
    form.name !== name || form.care_recipient_name !== careRecipient || form.timezone !== timezone

  return (
    <section className={styles.section}>
      <h2>This household</h2>
      {update.error ? <ErrorState error={update.error} title="That did not save" /> : null}
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
            onChange={(change) => setForm({ ...form, name: change.target.value })}
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
            onChange={(change) => setForm({ ...form, care_recipient_name: change.target.value })}
          />
          <span className={styles.hint}>Used in headings, and in every report Penny writes.</span>
        </div>

        <TimezoneField
          id="household-timezone"
          label="Timezone"
          value={form.timezone}
          onChange={(value) => setForm({ ...form, timezone: value })}
          hint="Every date in the history is shown in this zone, never the reader's — so a sibling abroad sees the same day as everyone else."
        />

        <div className={styles.actions}>
          <button
            type="submit"
            className={`${styles.button} ${styles.buttonPrimary}`}
            disabled={update.isPending || !dirty}
          >
            {update.isPending ? 'Saving…' : 'Save'}
          </button>
          {update.isSuccess && !dirty ? <span className={styles.ok}>Saved</span> : null}
        </div>
      </form>
    </section>
  )
}

/* ------------------------------------------------------------------------------- password ---- */

const MIN_PASSWORD = 12

function PasswordPanel() {
  const change = useChangePassword()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const tooShort = next.length > 0 && next.length < MIN_PASSWORD

  return (
    <section className={styles.section}>
      <h2>Family password</h2>
      <p className={styles.hint}>
        One password, shared by everyone who uses Penny. Changing it does not sign anybody out —
        you will need to pass the new one round yourself.
      </p>
      {change.error ? <ErrorState error={change.error} title="That did not change" /> : null}
      <form
        className={styles.form}
        onSubmit={(submit) => {
          submit.preventDefault()
          change.mutate(
            { current_password: current, new_password: next },
            {
              onSuccess: () => {
                setCurrent('')
                setNext('')
              },
            },
          )
        }}
      >
        <div className={styles.field}>
          <label className={styles.label} htmlFor="current-password">
            Current password
          </label>
          <input
            id="current-password"
            className={styles.input}
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(input) => setCurrent(input.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="new-password">
            New password
          </label>
          <input
            id="new-password"
            className={styles.input}
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(input) => setNext(input.target.value)}
          />
          <span className={tooShort ? styles.warnText : styles.hint}>
            At least {MIN_PASSWORD} characters. Three ordinary words beat one clever one.
          </span>
        </div>
        <div className={styles.actions}>
          <button
            type="submit"
            className={styles.button}
            disabled={change.isPending || current.length === 0 || next.length < MIN_PASSWORD}
          >
            {change.isPending ? 'Changing…' : 'Change password'}
          </button>
          {change.isSuccess ? <span className={styles.ok}>Changed</span> : null}
        </div>
      </form>
    </section>
  )
}

/* ------------------------------------------------------------------------------- whatsapp ---- */

/**
 * `is_connected`/`is_logged_in` are proxied from GOWA and are `false` when GOWA is simply
 * unreachable, which is not an error — so this panel never shows a failure for it, only a state.
 * A dead session is routine: WhatsApp expires linked devices, and the fix is to re-pair by QR.
 */
function whatsappSummary(status: WhatsappStatus): { tone: string | undefined; line: string } {
  // `gowa_available: false` and a dead session both arrive as `is_connected: false` and want
  // completely different sentences: one is our problem, the other needs a phone in someone's hand.
  if (status.gowa_available === false) {
    return {
      tone: 'warn',
      line: 'The WhatsApp bridge is not answering, so Penny cannot tell whether the link is alive. Nothing already saved is affected.',
    }
  }
  if (!status.linked) {
    return { tone: undefined, line: 'Not linked to a group yet.' }
  }
  if (!status.is_logged_in) {
    return {
      tone: 'warn',
      line: 'The WhatsApp session has ended. New messages are not arriving until the phone is paired again.',
    }
  }
  if (!status.is_connected) {
    return {
      tone: 'warn',
      line: 'Paired, but not connected right now. This usually recovers on its own within a minute.',
    }
  }
  return { tone: 'ok', line: 'Connected. New messages arrive within a few seconds of being sent.' }
}

function WhatsappPanel() {
  const status = useWhatsappStatus()
  const link = useLinkWhatsappGroup()
  const [chatId, setChatId] = useState('')

  return (
    <section className={styles.section}>
      <h2>WhatsApp</h2>
      <p className={styles.hint}>
        Linking the group is optional. Without it, Penny only knows what you import by hand; with
        it, new messages are picked up as they are sent.
      </p>

      {status.isPending ? <p className={styles.hint}>Checking…</p> : null}
      {status.error ? <ErrorState error={status.error} title="Couldn't check the link" /> : null}

      {status.data ? (
        <>
          <p className={statusClass(whatsappSummary(status.data).tone)}>
            {whatsappSummary(status.data).line}
          </p>
          <dl className={styles.facts}>
            <dt className={styles.factLabel}>Group</dt>
            <dd className={styles.factValue}>
              {status.data.group_external_id ? (
                <code>{status.data.group_external_id}</code>
              ) : (
                'none'
              )}
            </dd>
            <dt className={styles.factLabel}>Session</dt>
            <dd className={styles.factValue}>
              {status.data.is_logged_in ? 'paired' : 'not paired'} ·{' '}
              {status.data.is_connected ? 'connected' : 'not connected'}
            </dd>
          </dl>

          {!status.data.is_logged_in ? <RepairPanel /> : null}
        </>
      ) : null}

      {link.error ? <ErrorState error={link.error} title="That group was not linked" /> : null}

      <form
        className={styles.form}
        style={{ marginTop: 16 }}
        onSubmit={(submit) => {
          submit.preventDefault()
          link.mutate(chatId.trim(), { onSuccess: () => setChatId('') })
        }}
      >
        <div className={styles.field}>
          <label className={styles.label} htmlFor="chat-id">
            Group chat id
          </label>
          <input
            id="chat-id"
            className={styles.input}
            placeholder="120363000000000000@g.us"
            value={chatId}
            onChange={(input) => setChatId(input.target.value)}
          />
          <span className={styles.hint}>
            It always ends in <code>@g.us</code> — that suffix is the only thing that tells a group
            from a one-to-one chat, so anything else is refused.
          </span>
          {/* Groups the bridge has already heard from: the chat id is otherwise unfindable. */}
          {status.data?.unlinked_groups?.length ? (
            <div className={styles.suggestions}>
              <span className={styles.hint}>
                Penny has seen messages from these groups — one of them is probably yours:
              </span>
              {status.data.unlinked_groups.map((group) => (
                <button
                  key={group.chat_id}
                  type="button"
                  className={styles.linkButton}
                  onClick={() => setChatId(group.chat_id)}
                >
                  {group.chat_id} ({group.message_count.toLocaleString('en-GB')} messages)
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <div className={styles.actions}>
          <button
            type="submit"
            className={styles.button}
            disabled={link.isPending || chatId.trim().length === 0}
          >
            {link.isPending ? 'Linking…' : status.data?.linked ? 'Link a different group' : 'Link this group'}
          </button>
          {link.isSuccess ? <span className={styles.ok}>Linked</span> : null}
        </div>
      </form>
    </section>
  )
}

/**
 * Getting a fresh pairing QR on the screen.
 *
 * Deliberately a button rather than something the panel does on its own. The request blocks for
 * up to two minutes inside the bridge while whatsmeow produces the code, and the PNG it returns
 * is deleted about thirty seconds later — so requesting one nobody is standing in front of would
 * burn two minutes to produce an image that expires before anyone sees it. The countdown is
 * shown for the same reason: a stale QR that silently stops working looks like a broken product,
 * whereas one that says "expired, ask for another" is just a queue.
 */
function RepairPanel() {
  const relink = useWhatsappRelink()
  const [expiresAt, setExpiresAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (expiresAt === null) return
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [expiresAt])

  const secondsLeft = expiresAt === null ? 0 : Math.max(0, Math.ceil((expiresAt - now) / 1000))
  const qr = relink.data?.qr_link ?? null

  return (
    <div className={styles.callout}>
      <h3>Pair the phone again</h3>
      <p className={styles.hint}>
        On the phone that holds the family group: WhatsApp → Settings → Linked devices → Link a
        device. Then scan the code below. Nothing already saved is lost while it is unpaired.
      </p>

      {relink.error ? <ErrorState error={relink.error} title="No pairing code" /> : null}
      {relink.data && !relink.data.available ? (
        <p className={styles.warn} style={{ marginTop: 10 }}>
          {relink.data.error ?? 'The WhatsApp bridge could not produce a pairing code.'}
        </p>
      ) : null}

      {qr && secondsLeft > 0 ? (
        <figure className={styles.qr}>
          {/* A PNG URL, not a payload to encode — the bridge has already drawn it. */}
          <img src={qr} alt="WhatsApp pairing code" width={220} height={220} />
          <figcaption className={styles.hint}>Expires in {secondsLeft}s</figcaption>
        </figure>
      ) : null}
      {qr && secondsLeft === 0 ? (
        <p className={styles.hint} style={{ marginTop: 10 }}>
          That code has expired. Ask for another when the phone is ready.
        </p>
      ) : null}

      <div className={styles.actions} style={{ marginTop: 12 }}>
        <button
          type="button"
          className={styles.button}
          disabled={relink.isPending}
          onClick={() =>
            relink.mutate(undefined, {
              onSuccess: (result) => {
                setNow(Date.now())
                setExpiresAt(
                  result.qr_link ? Date.now() + (result.qr_duration ?? 30) * 1000 : null,
                )
              },
            })
          }
        >
          {relink.isPending ? 'Asking WhatsApp…' : qr ? 'Get another code' : 'Show the pairing code'}
        </button>
        {relink.isPending ? (
          <span className={styles.hint}>This can take up to two minutes. Keep the page open.</span>
        ) : null}
      </div>
    </div>
  )
}

function statusClass(tone: string | undefined): string {
  if (tone === 'ok') return styles.okLine ?? ''
  if (tone === 'warn') return styles.warn ?? ''
  return styles.hint ?? ''
}

/* -------------------------------------------------------------------------------- members ---- */

/**
 * A family that imports history AND pairs WhatsApp sees the same person twice: the export gives a
 * display name with no phone id, GOWA gives a phone id. That is expected rather than a bug, and
 * merging fixes attribution on every past message and event without re-running the LLM — which is
 * why it is offered here instead of being hidden behind a re-import.
 */
function MembersPanel({ timezone }: { timezone: string }) {
  const members = useMembers()
  const merge = useMergeMember()
  const [mergingId, setMergingId] = useState<string | null>(null)
  const [intoId, setIntoId] = useState('')

  const rows = members.data ?? []
  const merging = rows.find((member) => member.id === mergingId) ?? null

  return (
    <section className={styles.section}>
      <h2>People in the chat</h2>
      <p className={styles.hint}>
        Penny attributes each entry to whoever mentioned it. If one person appears twice, merge
        them and every past entry is re-attributed.
      </p>

      {members.isPending ? <p className={styles.hint}>Loading…</p> : null}
      {members.error ? <ErrorState error={members.error} title="Couldn't load the people" /> : null}
      {merge.error ? <ErrorState error={merge.error} title="That merge did not happen" /> : null}

      {members.isSuccess && rows.length === 0 ? (
        <p className={styles.hint}>
          Nobody yet — <Link to="/import">import a chat export</Link> and everyone who has written
          in the group will appear here.
        </p>
      ) : null}

      {rows.length > 0 ? (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Name</th>
              <th>Messages</th>
              <th>WhatsApp id</th>
              <th>Last seen</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((member) => (
              <tr key={member.id}>
                <td>{member.display_name}</td>
                <td>{member.message_count.toLocaleString('en-GB')}</td>
                <td>{describeId(member)}</td>
                <td>{formatDate(member.last_seen_at, timezone)}</td>
                <td>
                  <button
                    type="button"
                    className={styles.linkButton}
                    onClick={() => {
                      setMergingId(member.id === mergingId ? null : member.id)
                      setIntoId('')
                      merge.reset()
                    }}
                  >
                    {member.id === mergingId ? 'Cancel' : 'Merge'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {merging ? (
        <form
          className={styles.callout}
          onSubmit={(submit) => {
            submit.preventDefault()
            merge.mutate(
              { id: merging.id, into_member_id: intoId },
              {
                onSuccess: () => {
                  setMergingId(null)
                  setIntoId('')
                },
              },
            )
          }}
        >
          <h3>Merge {merging.display_name} into someone else</h3>
          <p className={styles.hint}>
            {merging.display_name} disappears; their {merging.message_count.toLocaleString('en-GB')}{' '}
            messages and every entry from them move across. This cannot be undone.
          </p>
          <div className={styles.actions} style={{ marginTop: 12 }}>
            <select
              className={styles.input}
              value={intoId}
              onChange={(change) => setIntoId(change.target.value)}
            >
              <option value="">Keep which person?</option>
              {rows
                .filter((candidate) => candidate.id !== merging.id)
                .map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.display_name} ({describeId(candidate)})
                  </option>
                ))}
            </select>
            <button
              type="submit"
              className={styles.button}
              disabled={intoId === '' || merge.isPending}
            >
              {merge.isPending ? 'Merging…' : 'Merge'}
            </button>
          </div>
        </form>
      ) : null}
    </section>
  )
}

function describeId(member: Member): string {
  if (member.wa_jid) return member.wa_jid
  if (member.wa_lid) return member.wa_lid
  return 'from an export — no phone id'
}
