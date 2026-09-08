import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { fetchMe, type User } from '../api/auth'
import { ApiError } from '../api/client'
import {
  clearSessionAuth,
  loadChatApiKeyForUser,
  getJwt,
  setApiKey as persistApiKey,
  setJwt,
  clearApiKey as removeApiKey,
  clearJwt,
} from './storage'

type AuthState = {
  token: string | null
  user: User | null
  apiKey: string | null
  loading: boolean
  setSession: (token: string, user: User) => void
  setChatApiKey: (key: string | null) => void
  logout: () => void
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => getJwt())
  const [user, setUser] = useState<User | null>(null)
  const [apiKey, setApiKeyState] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const restoreChatKey = useCallback((nextUser: User) => {
    setApiKeyState(loadChatApiKeyForUser(nextUser.id))
  }, [])

  const refreshUser = useCallback(async () => {
    const jwt = getJwt()
    if (!jwt) {
      setUser(null)
      setToken(null)
      setApiKeyState(null)
      setLoading(false)
      return
    }
    try {
      const me = await fetchMe(jwt)
      setToken(jwt)
      setUser(me)
      restoreChatKey(me)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // JWT 失效只清登录态，保留 Chat Key 供下次登录恢复
        clearSessionAuth()
        setToken(null)
        setUser(null)
        setApiKeyState(null)
      }
    } finally {
      setLoading(false)
    }
  }, [restoreChatKey])

  useEffect(() => {
    void refreshUser()
  }, [refreshUser])

  const setSession = useCallback(
    (jwt: string, nextUser: User) => {
      setJwt(jwt)
      setToken(jwt)
      setUser(nextUser)
      restoreChatKey(nextUser)
    },
    [restoreChatKey],
  )

  const setChatApiKey = useCallback(
    (key: string | null) => {
      const userId = user?.id
      if (key) {
        persistApiKey(key, userId)
        setApiKeyState(key)
      } else {
        removeApiKey(userId)
        setApiKeyState(null)
      }
    },
    [user?.id],
  )

  const logout = useCallback(() => {
    clearJwt()
    setToken(null)
    setUser(null)
    // 内存清空；localStorage 中按用户保存的 Chat Key 保留
    setApiKeyState(null)
  }, [])

  const value = useMemo(
    () => ({
      token,
      user,
      apiKey,
      loading,
      setSession,
      setChatApiKey,
      logout,
      refreshUser,
    }),
    [token, user, apiKey, loading, setSession, setChatApiKey, logout, refreshUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
