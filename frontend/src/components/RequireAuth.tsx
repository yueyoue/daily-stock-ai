import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { authApi, type SessionUser } from '../api/client'

export const UserContext = createContext<SessionUser | null>(null)
export const useCurrentUser = () => useContext(UserContext)

/** 路由守卫（开发文档 §3.4）：未登录跳登录页并记住来源路径，登录后回跳 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const [state, setState] = useState<'checking' | 'ok' | 'anon'>('checking')
  const [user, setUser] = useState<SessionUser | null>(null)
  const location = useLocation()

  useEffect(() => {
    authApi
      .session()
      .then((u) => {
        setUser(u)
        setState('ok')
      })
      .catch(() => setState('anon'))
  }, [])

  if (state === 'checking') return null
  if (state === 'anon') {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return <UserContext.Provider value={user}>{children}</UserContext.Provider>
}
