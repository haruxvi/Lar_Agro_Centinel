import { createBrowserRouter } from 'react-router-dom'

import { ROUTES } from '@/config/routes'
import { LoginPage } from '@/pages/auth/LoginPage'
import { NotFoundPage } from '@/pages/NotFoundPage'

export const router = createBrowserRouter([
  { path: ROUTES.LOGIN, element: <LoginPage /> },
  { path: '*', element: <NotFoundPage /> },
])
