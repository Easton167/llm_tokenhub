import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { apiRequest } from '../api/client'
import { ApiError } from '../api/client'
import { clearAdminJwt, getAdminJwt, setAdminJwt } from './adminStorage'

export type AdminMenu = {
  key: string
  name: string
  path: string
  icon: string
}

export type AdminUser = {
  username: string
  is_superadmin: boolean
  /** 形如 channel:view / wallet:manage，由后端按角色汇总 */
  permissions: string[]
  /** 已按 <key>:view 权限过滤过的侧边栏项 */
  menus: AdminMenu[]
}

type AdminAuthState = {
  token: string | null
  admin: AdminUser | null
  loading: boolean
  /** 按权限码判断按钮是否可用；超管恒为 true。仅用于隐藏 UI，真正的拦截在后端 */
  can: (code: string) => boolean
  setSession: (token: string, admin: AdminUser) => void
  logout: () => void
  refresh: () => Promise<void>
}

const AdminAuthContext = createContext<AdminAuthState | null>(null)

export function AdminAuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => getAdminJwt())
  const [admin, setAdmin] = useState<AdminUser | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const jwt = getAdminJwt()
    if (!jwt) {
      setToken(null)
      setAdmin(null)
      setLoading(false)
      return
    }
    try {
      const me = await apiRequest<AdminUser>('/admin/auth/me', { token: jwt })
      setToken(jwt)
      setAdmin(me)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearAdminJwt()
        setToken(null)
        setAdmin(null)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const setSession = useCallback((jwt: string, next: AdminUser) => {
    setAdminJwt(jwt)
    setToken(jwt)
    setAdmin(next)
  }, [])

  const logout = useCallback(() => {
    clearAdminJwt()
    setToken(null)
    setAdmin(null)
  }, [])

  const can = useCallback(
    (code: string) => !!admin && (admin.is_superadmin || admin.permissions.includes(code)),
    [admin],
  )

  const value = useMemo(
    () => ({ token, admin, loading, can, setSession, logout, refresh }),
    [token, admin, loading, can, setSession, logout, refresh],
  )

  return <AdminAuthContext.Provider value={value}>{children}</AdminAuthContext.Provider>
}

/** 登录后的落地页：取第一个有权访问的菜单，不能写死某个页面 */
export function adminHomePath(admin: AdminUser | null): string {
  if (!admin) return '/admin/login'
  // 一个菜单都没有时落到 /admin，由 AdminLayout 给出提示；
  // 若在这里回 /admin/login 会和登录页的"已登录就跳走"互相弹，转成死循环
  return admin.menus[0]?.path ?? '/admin'
}

export function useAdminAuth(): AdminAuthState {
  const ctx = useContext(AdminAuthContext)
  if (!ctx) throw new Error('useAdminAuth must be used within AdminAuthProvider')
  return ctx
}
