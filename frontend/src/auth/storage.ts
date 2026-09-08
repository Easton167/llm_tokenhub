const JWT_KEY = 'tokenhub_jwt'
/** 旧版全局 Chat Key；登录后迁移到按用户隔离的键并删除 */
const LEGACY_API_KEY = 'tokenhub_api_key'

function chatKeyStorageKey(userId: string): string {
  return `tokenhub_chat_api_key:${userId}`
}

export function getJwt(): string | null {
  return localStorage.getItem(JWT_KEY)
}

export function setJwt(token: string): void {
  localStorage.setItem(JWT_KEY, token)
}

export function clearJwt(): void {
  localStorage.removeItem(JWT_KEY)
}

/** 读取指定用户的 Chat Key（不含跨用户回退）。 */
export function getApiKey(userId?: string | null): string | null {
  if (userId) {
    return localStorage.getItem(chatKeyStorageKey(userId))
  }
  return localStorage.getItem(LEGACY_API_KEY)
}

/**
 * 登录后调用：优先读用户隔离 key；若无则把旧版全局 key 迁到该用户下。
 */
export function loadChatApiKeyForUser(userId: string): string | null {
  const scoped = localStorage.getItem(chatKeyStorageKey(userId))
  if (scoped) return scoped

  const legacy = localStorage.getItem(LEGACY_API_KEY)
  if (!legacy) return null

  localStorage.setItem(chatKeyStorageKey(userId), legacy)
  localStorage.removeItem(LEGACY_API_KEY)
  return legacy
}

export function setApiKey(key: string, userId?: string | null): void {
  if (userId) {
    localStorage.setItem(chatKeyStorageKey(userId), key)
    localStorage.removeItem(LEGACY_API_KEY)
    return
  }
  localStorage.setItem(LEGACY_API_KEY, key)
}

export function clearApiKey(userId?: string | null): void {
  if (userId) {
    localStorage.removeItem(chatKeyStorageKey(userId))
  }
  localStorage.removeItem(LEGACY_API_KEY)
}

/** 仅清除登录态；Chat Key 持久保留，下次登录同一用户自动恢复 */
export function clearSessionAuth(): void {
  clearJwt()
}
