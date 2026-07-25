import { useState } from 'react'

import { USE_FIXTURES } from '../api/client'
import { useLogin } from '../api/queries'
import styles from './routes.module.css'

/**
 * One shared family credential — there is no users table. Feed attribution comes from the
 * WhatsApp sender name on each message, not from who signed in, so the product premise survives.
 *
 * The failure message is whatever the server said, rendered verbatim: an unknown family name and
 * a wrong password produce the identical sentence (no enumeration), and being locked out for a
 * minute after too many attempts produces its own — one shared secret with no lockout story is
 * exactly the thing brute force is for, so the rate limit is real and saying so is kinder than a
 * generic failure. Never retried: a 4xx retried is the same 4xx, and mutations don't retry at all.
 *
 * No redirect on success either: signing in resets the cache, the session query refetches, and
 * the layout's gate moves us off /login. One rule, one place.
 */
export function LoginRoute() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const login = useLogin()

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Sign in</h1>
        <p className={styles.hint}>
          Penny has one password for the whole family, not an account each.
        </p>
      </div>

      {login.error ? (
        <p className={styles.error} role="alert">
          {login.error.message}
        </p>
      ) : null}

      <form
        className={styles.form}
        onSubmit={(submit) => {
          submit.preventDefault()
          login.mutate({ username, password })
        }}
      >
        <div className={styles.field}>
          <label className={styles.label} htmlFor="username">
            Family name
          </label>
          <input
            id="username"
            className={styles.input}
            autoComplete="username"
            value={username}
            onChange={(change) => setUsername(change.target.value)}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            className={styles.input}
            autoComplete="current-password"
            value={password}
            onChange={(change) => setPassword(change.target.value)}
          />
        </div>

        <div className={styles.actions}>
          <button
            type="submit"
            className={`${styles.button} ${styles.buttonPrimary}`}
            disabled={login.isPending || username.trim() === '' || password === ''}
          >
            {login.isPending ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
      </form>

      {USE_FIXTURES ? (
        <p className={styles.hint} style={{ marginTop: 20 }}>
          Demo build: sign in as <code>the-doyles</code> /{' '}
          <code>correct-horse-battery-staple</code>.
        </p>
      ) : null}
    </>
  )
}
