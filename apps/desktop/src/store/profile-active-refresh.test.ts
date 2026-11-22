import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

// /api/profiles/active carries the same data on every read for a given
// (connection, profile) scope, and boot + the activeGatewayProfile effect +
// the rail's focus refresh all ask for it in the same second. Measured on an
// idle Webapp tab these landed as bursts of 4 requests. One shared in-flight
// read replaces the burst; a later caller after it settles reads again, so a
// profile renamed elsewhere still shows up.

const hermesApi = vi.fn(async (_request?: { path?: string }) => ({ active: 'default', current: 'default' }))
const getProfiles = vi.fn(async () => ({ profiles: [] }))

vi.mock('@/store/gateway', () => ({
  $gateway: atom({ id: 'live-socket', connectionState: 'open' }),
  activeGateway: () => null,
  activeGatewayConnectionId: () => null,
  activeGatewayProfileKey: () => 'default',
  ensureGatewayForAgent: vi.fn(async () => undefined),
  ensureGatewayForProfile: vi.fn(async () => undefined),
  openGatewayForAgent: vi.fn(async () => undefined),
  openGatewayForProfile: vi.fn(async () => undefined),
  openSecondaryCount: () => 0
}))
vi.mock('@/hermes', () => ({
  getProfiles,
  hermesApi,
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 1_000
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ reloadOpenStarmapGraph: vi.fn(), resetStarmapGraph: vi.fn() }))

const { invalidateProfileListFetches, refreshActiveProfile } = await import('./profile')

const activeReads = () =>
  hermesApi.mock.calls.filter(call => (call[0] as { path?: string } | undefined)?.path === '/api/profiles/active').length

const localConn = (): HermesConnection =>
  ({ baseUrl: 'http://127.0.0.1:8642', connectionId: 'local', mode: 'local' }) as HermesConnection

beforeEach(() => {
  vi.stubGlobal('window', { hermesDesktop: { getConnection: vi.fn(async () => localConn()) } })
  invalidateProfileListFetches()
  hermesApi.mockClear()
  getProfiles.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('refreshActiveProfile single-flight', () => {
  it('shares one /api/profiles/active read between concurrent callers', async () => {
    await Promise.all([refreshActiveProfile(), refreshActiveProfile(), refreshActiveProfile(), refreshActiveProfile()])

    expect(activeReads()).toBe(1)
  })

  it('reads again once the shared flight has settled', async () => {
    await refreshActiveProfile()
    await refreshActiveProfile()

    expect(activeReads()).toBe(2)
  })

  it('keeps sharing the profile list read too (refreshProfiles single-flight)', async () => {
    await Promise.all([refreshActiveProfile(), refreshActiveProfile()])

    expect(getProfiles).toHaveBeenCalledTimes(1)
  })
})
