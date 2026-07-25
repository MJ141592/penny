import { createBrowserRouter, RouterProvider } from 'react-router'

import { FeedRoute } from './routes/feed'
import { ImportRoute } from './routes/import'
import { Layout } from './routes/layout'
import { LoginRoute } from './routes/login'
import { NotFoundRoute, RouteErrorBoundary } from './routes/not-found'
import { ReportRoute } from './routes/report'
import { SettingsRoute } from './routes/settings'

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
