const TOKEN_PREFIX = 'bims-portal-token:'

export function loadToken(slug: string): string | null {
  return sessionStorage.getItem(TOKEN_PREFIX + slug)
}

export function saveToken(slug: string, token: string): void {
  sessionStorage.setItem(TOKEN_PREFIX + slug, token)
}

export function clearToken(slug: string): void {
  sessionStorage.removeItem(TOKEN_PREFIX + slug)
}
