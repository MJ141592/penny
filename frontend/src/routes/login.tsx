import { useState } from 'react'

import { USE_FIXTURES } from '../api/client'
import { useLogin } from '../api/queries'
import styles from './routes.module.css'

/**
 * One shared family credential — there is no users table. Feed attribution comes from the
 * WhatsApp sender name on each message, not from who signed in, so the product premise survives.
 *
 * No redirect on success: `useLogin` clears the cache, the session query refetches, and the
 * layout's gate moves us off /login. One rule, one place.
 */
export function LoginRoute() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const login = useLogin()

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Sign in</h1>
        <p className={styles.hint}>The family password, shared in the group chat.</p>
      </div>

      {login.error ? <p className={styles.error}>{login.error.message}</p> : null}

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
            disabled={login.isPending}
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
