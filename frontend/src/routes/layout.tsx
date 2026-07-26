import { NavLink, Navigate, Outlet, useLocation } from 'react-router'

import { USE_FIXTURES } from '../api/client'
import { useLogout, useSession } from '../api/queries'
import styles from './routes.module.css'

const NAV = [
  { to: '/', label: 'History', end: true },
  { to: '/import', label: 'Import' },
  { to: '/settings', label: 'Settings' },
]

/**
 * The shell every screen renders inside, and the one place the session gate lives.
 *
 * A 401 from `GET /api/me` means signed out, and this redirects to /welcome — visitors get the
 * marketing page, and existing families reach the login form from its Sign in link. Doing it here
 * rather than per route means a new route cannot forget to be protected, and it is why the retry
 * policy matters: retrying the 401 would stall on a spinner before the redirect ever happened.
 */
export function Layout() {
  const session = useSession()
  const logout = useLogout()
  const location = useLocation()

  const onLoginScreen = location.pathname === '/login'
  const signedOut = !session.isPending && session.data === null

  return (
    <div className={styles.shell}>
      {/* Nobody should ever mistake the offline demo for their own family's history. */}
      {USE_FIXTURES ? (
        <p className={styles.demoBanner}>
          Demo data — this build is not connected to a backend. Everything below is invented.
        </p>
      ) : null}
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <NavLink to="/" className={styles.brand}>
            Penny
            <span className={styles.brandTag}>Care Record</span>
            {session.data ? (
              <span className={styles.brandSub}>{session.data.household.name}</span>
            ) : null}
          </NavLink>
          {session.data ? (
            <nav className={styles.nav}>
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
                  }
                >
                  {item.label}
                </NavLink>
              ))}
              <button
                type="button"
                className={styles.button}
                disabled={logout.isPending}
                onClick={() => logout.mutate()}
              >
                Sign out
              </button>
            </nav>
          ) : null}
        </div>
      </header>

      <main className={styles.main}>
        <Gate isPending={session.isPending} signedOut={signedOut} onLoginScreen={onLoginScreen} />
      </main>
    </div>
  )
}

function Gate({
  isPending,
  signedOut,
  onLoginScreen,
}: {
  isPending: boolean
  signedOut: boolean
  onLoginScreen: boolean
}) {
  if (isPending) return <p className={styles.hint}>Loading…</p>
  if (signedOut && !onLoginScreen) return <Navigate to="/welcome" replace />
  if (!signedOut && onLoginScreen) return <Navigate to="/" replace />
  return <Outlet />
}
