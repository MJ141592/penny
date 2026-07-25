import { Link, useRouteError } from 'react-router'

import styles from './routes.module.css'

export function NotFoundRoute() {
  return (
    <div className={styles.empty}>
      <h2>That page does not exist</h2>
      <p>
        <Link to="/">Back to the feed</Link>
      </p>
    </div>
  )
}

/**
 * The router's last line of defence. A render-time throw (a bad param, a fixture that does not
 * match the contract) lands here instead of unmounting the app to a white screen.
 */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const message = error instanceof Error ? error.message : 'Something went wrong.'
  return (
    <div className={styles.shell}>
      <div className={styles.main}>
        <div className={styles.empty}>
          <h2>Something went wrong</h2>
          <p>{message}</p>
          <p>
            <a href="/">Reload Penny</a>
          </p>
        </div>
      </div>
    </div>
  )
}
