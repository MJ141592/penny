/**
 * The first thing a family sees after following the link Penny posted into their WhatsApp group.
 *
 * They arrive holding a username and a passphrase, on a household that has a placeholder name and
 * an empty history. An empty feed would tell them nothing, so this replaces it with two screens:
 * say who is being cared for, then read what happens next. Nothing here is a wall — the nav is
 * still up and every other route is reachable — but the root sends them here until the household
 * has a real name, because that name is the single biggest input to extraction quality.
 *
 * Two steps rather than one form, because they are two different jobs: the first is the only
 * thing we need from them, the second is the only thing they need from us.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { useChangePassword, useSession, useUpdateHousehold } from '../api/queries'
import { ErrorState } from '../components'
import { formatEvidenceStamp } from '../lib/datetime'
import { isPlaceholderCareRecipient } from '../lib/first-run'
import type { Household } from '../types/api'
import routes from './routes.module.css'
import { TimezoneField } from './timezone-field'
import styles from './welcome.module.css'

export function WelcomeRoute() {
  const session = useSession()

  if (session.isPending) return <p className={routes.hint}>Loading…</p>
  // Signed out is the layout's problem, and it is already redirecting to /login.
  if (!session.data) return null

  return (
    <FirstRun
      key={session.data.household.id}
      household={session.data.household}
      messageCount={session.data.counts.messages}
    />
  )
}

type Step = 'identity' | 'next'

/**
 * Keyed on the household id by the caller, so the step and every draft field are thrown away if
 * the session changes underneath — signing out and back in as another family on the same laptop
 * must not leave a half-typed name in the box.
 *
 * The initial step is decided ONCE, from the state the household arrived in. It deliberately does
 * not track the session afterwards: saving the name is what moves us to step two, and if this
 * recomputed itself the successful save would skip the "what happens next" screen entirely — the
 * one screen that explains what the family just signed up to.
 */
function FirstRun({ household, messageCount }: { household: Household; messageCount: number }) {
  const [step, setStep] = useState<Step>(() =>
    isPlaceholderCareRecipient(household.care_recipient_name) ? 'identity' : 'next',
  )

  return (
    <div className={styles.wrap}>
      {step === 'identity' ? (
        <IdentityStep household={household} onSaved={() => setStep('next')} />
      ) : (
        <NextStep household={household} messageCount={messageCount} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ step one: who, and where -- */

function IdentityStep({ household, onSaved }: { household: Household; onSaved: () => void }) {
  const update = useUpdateHousehold()
  const placeholder = isPlaceholderCareRecipient(household.care_recipient_name)
  const [name, setName] = useState(placeholder ? '' : household.care_recipient_name)
  const [timezone, setTimezone] = useState(household.timezone)

  const trimmed = name.trim()
  const usable = trimmed.length > 0 && !isPlaceholderCareRecipient(trimmed)

  return (
    <>
      <p className={styles.eyebrow}>Step 1 of 2</p>
      <h1>Who is Penny keeping track of?</h1>
      <p className={styles.lede}>
        Penny reads your family's WhatsApp group and writes down what actually happens — the
        appointments, the symptoms, the medication changes — so nobody has to remember it all.
      </p>

      <section className={styles.card}>
        {update.error ? <ErrorState error={update.error} title="That did not save" /> : null}
        <form
          className={routes.form}
          onSubmit={(submit) => {
            submit.preventDefault()
            if (!usable) return
            update.mutate({ care_recipient_name: trimmed, timezone }, { onSuccess: onSaved })
          }}
        >
          <div className={routes.field}>
            <label className={routes.label} htmlFor="welcome-care-recipient">
              The person being cared for
            </label>
            <input
              id="welcome-care-recipient"
              className={routes.input}
              autoFocus
              autoComplete="off"
              placeholder="Margaret"
              value={name}
              onChange={(change) => setName(change.target.value)}
            />
            <span className={routes.hint}>
              First name is enough — whatever the family actually calls them in the chat.
            </span>
          </div>

          {/*
           * The one paragraph on this screen that has to land. A family that understands why the
           * name matters types a real one; a family that doesn't types "Mum" or skips it, and
           * every extraction afterwards is worse for it.
           */}
          <p className={styles.why}>
            This is the single biggest thing that decides how much Penny gets right: knowing the
            name is what turns “she had a bad night” in the group chat into a recorded fact about{' '}
            <strong>{usable ? trimmed : 'the person you care for'}</strong>, instead of a sentence
            Penny has to guess at.
          </p>

          <TimezoneField
            id="welcome-timezone"
            label="Timezone"
            value={timezone}
            onChange={setTimezone}
            hint="Every date Penny shows is in this zone, not the reader's — so a sibling abroad sees the same day as everyone at home."
          />

          <TimezoneCheck timezone={timezone} onUse={setTimezone} />

          <div className={routes.actions}>
            <button
              type="submit"
              className={`${routes.button} ${routes.buttonPrimary}`}
              disabled={update.isPending || !usable}
            >
              {update.isPending ? 'Saving…' : 'Save and continue'}
            </button>
          </div>
        </form>
      </section>
    </>
  )
}

/**
 * "Confirm the timezone" as something a person can actually check, rather than a name in a select
 * that most people have never had to think about. It prints the time it is *right now* in the
 * chosen zone — through `Intl`, in the household's zone, never the browser's — so the confirmation
 * is "yes, that's my clock" instead of "yes, that's probably my continent".
 *
 * The browser's own zone is offered as a one-click suggestion and never applied silently: it is
 * the reader's zone, which is exactly the thing the rest of the app refuses to render dates in.
 */
function TimezoneCheck({ timezone, onUse }: { timezone: string; onUse: (zone: string) => void }) {
  const [now, setNow] = useState(() => new Date())

  // The whole point is that the clock is current; a stamp frozen at mount is the thing being
  // checked against a wall clock, so it has to keep up.
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(timer)
  }, [])

  const browserZone = browserTimezone()

  return (
    <div className={styles.clock}>
      <span className={styles.clockLabel}>Right now, there</span>
      <span className={styles.clockStamp}>{formatEvidenceStamp(now.toISOString(), timezone)}</span>
      {browserZone && browserZone !== timezone ? (
        <button type="button" className={routes.linkButton} onClick={() => onUse(browserZone)}>
          This device is set to {browserZone.replace(/_/g, ' ')} — use that instead
        </button>
      ) : null}
    </div>
  )
}

function browserTimezone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null
  } catch {
    return null
  }
}

/* --------------------------------------------------------------- step two: what happens next -- */

function NextStep({ household, messageCount }: { household: Household; messageCount: number }) {
  const name = isPlaceholderCareRecipient(household.care_recipient_name)
    ? 'them'
    : household.care_recipient_name

  return (
    <>
      <p className={styles.eyebrow}>Step 2 of 2</p>
      <h1>Penny is listening</h1>
      <p className={styles.lede}>
        Nothing else has to be set up. Here is what happens from here.
      </p>

      <ol className={styles.next}>
        <li>
          <strong>Keep talking in the group.</strong> Penny reads every new message in the WhatsApp
          group she was added to. Nobody has to write anything twice, or write to Penny at all.
        </li>
        <li>
          <strong>Entries appear on their own.</strong> When someone mentions a GP appointment, a
          bad night or a change of tablets, it turns up in {name}'s history within a minute or two —
          with the original message quoted underneath it, so you can always see where it came from.
        </li>
        <li>
          <strong>Bring the back history if you want it.</strong> Penny starts from today.{' '}
          {messageCount === 0
            ? 'Everything the family said before now is still only in WhatsApp.'
            : `${messageCount.toLocaleString('en-GB')} messages are already in.`}{' '}
          Export the group chat from WhatsApp and import the <code>.txt</code> file to fill in
          everything up to now.
        </li>
      </ol>

      <div className={routes.actions}>
        <Link to="/" className={`${routes.button} ${routes.buttonPrimary}`}>
          Go to {name === 'them' ? 'the history' : `${name}'s history`}
        </Link>
        <Link to="/import" className={routes.button}>
          Import an existing chat export
        </Link>
      </div>

      <PasswordPrompt />

      <p className={styles.footnote}>
        The name, the timezone and the family name can all be changed later in{' '}
        <Link to="/settings">Settings</Link>.
      </p>
    </>
  )
}

/* ------------------------------------------------------------------- the password to replace -- */

const MIN_PASSWORD = 12

/**
 * A recommendation, not a gate.
 *
 * Penny posted the password into the group chat, which is what proved the family owned the group
 * in the first place — but it also means it is sitting in the scrollback forever, readable by
 * anyone added to that group next year. That is worth fixing on day one and is not worth blocking
 * anybody over, so it sits under the "go to the history" buttons, and skipping it costs nothing.
 */
function PasswordPrompt() {
  const change = useChangePassword()
  const [open, setOpen] = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const tooShort = next.length > 0 && next.length < MIN_PASSWORD

  if (change.isSuccess) {
    return (
      <section className={styles.recommend}>
        <h2>Password changed</h2>
        <p className={routes.hint}>
          The one Penny posted into the group no longer works. Pass the new one round however your
          family normally would — nobody has been signed out.
        </p>
      </section>
    )
  }

  return (
    <section className={styles.recommend}>
      <h2>Worth doing today: change the shared password</h2>
      <p className={styles.recommendBody}>
        Penny sent the password into your WhatsApp group, which is how she knew it was reaching the
        right family. It is also still in the group's scrollback, so anyone added to the chat later
        can read it. Changing it now does not sign anybody out.
      </p>

      {open ? (
        <>
          {change.error ? <ErrorState error={change.error} title="That did not change" /> : null}
          <form
            className={routes.form}
            onSubmit={(submit) => {
              submit.preventDefault()
              change.mutate({ current_password: current, new_password: next })
            }}
          >
            <div className={routes.field}>
              <label className={routes.label} htmlFor="welcome-current-password">
                The password from the group message
              </label>
              <input
                id="welcome-current-password"
                className={routes.input}
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(input) => setCurrent(input.target.value)}
              />
            </div>
            <div className={routes.field}>
              <label className={routes.label} htmlFor="welcome-new-password">
                New password
              </label>
              <input
                id="welcome-new-password"
                className={routes.input}
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(input) => setNext(input.target.value)}
              />
              <span className={tooShort ? routes.warnText : routes.hint}>
                At least {MIN_PASSWORD} characters. Three ordinary words beat one clever one.
              </span>
            </div>
            <div className={routes.actions}>
              <button
                type="submit"
                className={routes.button}
                disabled={change.isPending || current.length === 0 || next.length < MIN_PASSWORD}
              >
                {change.isPending ? 'Changing…' : 'Change password'}
              </button>
              <button
                type="button"
                className={routes.linkButton}
                onClick={() => {
                  setOpen(false)
                  setCurrent('')
                  setNext('')
                  change.reset()
                }}
              >
                Not now
              </button>
            </div>
          </form>
        </>
      ) : (
        <div className={routes.actions}>
          <button type="button" className={routes.button} onClick={() => setOpen(true)}>
            Change it now
          </button>
          <span className={routes.hint}>
            Or leave it — it is under Family password in <Link to="/settings">Settings</Link>{' '}
            whenever you want it.
          </span>
        </div>
      )}
    </section>
  )
}
