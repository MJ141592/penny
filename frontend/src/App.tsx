import { createBrowserRouter, RouterProvider } from 'react-router'

import { FeedRoute } from './routes/feed'
import { ImportRoute } from './routes/import'
import { Layout } from './routes/layout'
import { LoginRoute } from './routes/login'
import { NotFoundRoute, RouteErrorBoundary } from './routes/not-found'
import { ReportRoute } from './routes/report'
import { SettingsRoute } from './routes/settings'
import { WelcomeRoute } from './routes/welcome'

/**
 * Client-side routing only. The SPA is served from the same origin as the API, so every path
 * below has to fall back to `index.html` on the server — that is a deploy-config obligation, not
 * something the router can fix.
 */
const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    errorElement: <RouteErrorBoundary />,
    children: [
      { index: true, element: <FeedRoute /> },
      { path: 'login', element: <LoginRoute /> },
      // Where a family that followed the link out of their WhatsApp group lands. Reachable on its
      // own so the setup step can be re-opened, and redirected to from `/` while the household
      // still has no care recipient — see the layout's Gate.
      { path: 'welcome', element: <WelcomeRoute /> },
      { path: 'settings', element: <SettingsRoute /> },
      { path: 'import', element: <ImportRoute /> },
      { path: 'reports/:id', element: <ReportRoute /> },
      { path: '*', element: <NotFoundRoute /> },
    ],
  },
])

export function App() {
  return <RouterProvider router={router} />
}
