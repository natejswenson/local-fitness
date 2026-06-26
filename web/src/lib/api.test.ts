import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, authToken, AuthRequiredError, onAuthRequired } from '@/lib/api'

// authedFetch / withAuth are module-private; we drive them through the public
// `api` surface (every method routes through authedFetch), mocking global.fetch
// so we can inspect exactly what headers go on the wire and how a 401 is handled.

const TOKEN = 'secret-token-abc123'

function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('authedFetch bearer attachment', () => {
  it('attaches Authorization: Bearer <token> when a token is in localStorage', async () => {
    authToken.set(TOKEN)
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { ok: true, auth_required: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api.authVerify()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/auth/verify')
    const headers = new Headers((init as RequestInit | undefined)?.headers)
    expect(headers.get('Authorization')).toBe(`Bearer ${TOKEN}`)
  })

  it('omits the Authorization header when no token is present', async () => {
    authToken.clear()
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { ok: true, auth_required: false }))
    vi.stubGlobal('fetch', fetchMock)

    await api.authVerify()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, init] = fetchMock.mock.calls[0]
    const headers = new Headers((init as RequestInit | undefined)?.headers)
    expect(headers.has('Authorization')).toBe(false)
  })
})

describe('authedFetch 401 handling', () => {
  it('clears the token, notifies onAuthRequired listeners, and throws AuthRequiredError', async () => {
    authToken.set(TOKEN)
    const handler = vi.fn()
    const off = onAuthRequired(handler)
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, {}))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.status()).rejects.toBeInstanceOf(AuthRequiredError)

    expect(handler).toHaveBeenCalledTimes(1)
    // The wrapper clears the rejected token so the AuthGate re-prompts fresh.
    expect(authToken.get()).toBeNull()
    off()
  })
})
