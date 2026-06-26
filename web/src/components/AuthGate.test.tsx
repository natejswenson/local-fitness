import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AuthGate } from '@/components/AuthGate'
import { api, authToken, AuthRequiredError } from '@/lib/api'

// Drive the gate through the REAL api/authedFetch by mocking global.fetch, so
// these tests pin the actual auth wiring (probe on mount + mid-session 401
// re-prompt via onAuthRequired), not a stubbed-out api module.

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

describe('AuthGate', () => {
  it('renders children once the auth probe succeeds', async () => {
    authToken.set('good-token')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(fakeResponse(200, { ok: true, auth_required: true })),
    )

    render(
      <AuthGate>
        <div>secret dashboard</div>
      </AuthGate>,
    )

    expect(await screen.findByText('secret dashboard')).toBeInTheDocument()
  })

  it('shows the token-entry prompt when a later request returns 401 mid-session', async () => {
    authToken.set('good-token')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(fakeResponse(200, { ok: true, auth_required: true })),
    )

    render(
      <AuthGate>
        <div>secret dashboard</div>
      </AuthGate>,
    )
    await screen.findByText('secret dashboard')

    // Server token rotated: the next API call 401s. authedFetch fires
    // onAuthRequired, which the mounted AuthGate listens for and re-prompts.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(fakeResponse(401, {})))
    await expect(api.status()).rejects.toBeInstanceOf(AuthRequiredError)

    expect(await screen.findByText(/requires an API token/i)).toBeInTheDocument()
    expect(screen.queryByText('secret dashboard')).not.toBeInTheDocument()
  })
})
