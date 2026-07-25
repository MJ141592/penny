import { NavLink, Navigate, Outlet, useLocation } from 'react-router'

import { USE_FIXTURES } from '../api/client'
import { useLogout, useSession } from '../api/queries'
import { needsFirstRunSetup } from '../lib/first-run'
import styles from './routes.module.css'

const NAV = [
  { to: '/', label: 'History', end: true },
  { to: '/import', label: 'Import' },
  { to: '/settings', label: 'Settings' },
]

/**
 * While the household still has no care recipient, `/` bounces to `/welcome` — so a "History"
 * link would look broken, clicking it and landing back on the same screen. Name the destination
 * it actually has instead.
 */
const SETUP_NAV = [{ to: '/welcome', label: 'Set up', end: true }, ...NAV.slice(1)]

/**
 * The shell every screen renders inside, and the one place the session gate lives.
 *
 * A 401 from `GET /api/me` means signed out, and this redirects to /login. Doing it here rather
 * than per route means a new route cannot forget to be protected, and it is why the retry policy
 * matters: retrying the 401 would stall on a spinner before the redirect ever happened.
 */
export function Layout() {
  const session = useSession()
  const logout = useLogout()
  const location = useLocation()

  const onLoginScreen = location.pathname === '/login'
  const signedOut = !session.isPending && session.data === null
  // A household provisioned from a WhatsApp group has no care recipient name and no history yet.
  const needsSetup = needsFirstRunSetup(session.data)

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
            {session.data ? (
              <span className={styles.brandSub}>{session.data.household.name}</span>
            ) : null}
          </NavLink>
          {session.data ? (
            <nav className={styles.nav}>
              {(needsSetup ? SETUP_NAV : NAV).map((item) => (
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
        <Gate
          isPending={session.isPending}
          signedOut={signedOut}
          onLoginScreen={onLoginScreen}
          needsSetup={needsSetup}
          atRoot={location.pathname === '/'}
        />
      </main>
    </div>
  )
}

function Gate({
  isPending,
  signedOut,
  onLoginScreen,
  needsSetup,
  atRoot,
}: {
  isPending: boolean
  signedOut: boolean
  onLoginScreen: boolean
  needsSetup: boolean
  atRoot: boolean
}) {
  if (isPending) return <p className={styles.hint}>Loading…</p>
  if (signedOut && !onLoginScreen) return <Navigate to="/login" replace />
  if (!signedOut && onLoginScreen) return <Navigate to="/" replace />
  /*
   * A family that has just followed the link out of their WhatsApp group would otherwise land on
   * an empty feed with no explanation. Send them to the setup step instead — but only from the
   * root, so this is a starting point and not a wall: the nav stays up and Settings, Import and
   * the feed itself are all still reachable by anyone who would rather look around first.
   */
  if (needsSetup && atRoot) return <Navigate to="/welcome" replace />
  return <Outlet />
}
