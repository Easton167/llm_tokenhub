const ADMIN_JWT_KEY = 'tokenhub_admin_jwt'

export function getAdminJwt(): string | null {
  return localStorage.getItem(ADMIN_JWT_KEY)
}

export function setAdminJwt(token: string): void {
  localStorage.setItem(ADMIN_JWT_KEY, token)
}

export function clearAdminJwt(): void {
  localStorage.removeItem(ADMIN_JWT_KEY)
}
