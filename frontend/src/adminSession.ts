const ADMIN_TOKEN_PREFIX = 'bims-admin-token:'

export function loadAdminToken(slug: string): string | null {
  return sessionStorage.getItem(ADMIN_TOKEN_PREFIX + slug)
}

export function saveAdminToken(slug: string, token: string): void {
  sessionStorage.setItem(ADMIN_TOKEN_PREFIX + slug, token)
}

export function clearAdminToken(slug: string): void {
  sessionStorage.removeItem(ADMIN_TOKEN_PREFIX + slug)
}
