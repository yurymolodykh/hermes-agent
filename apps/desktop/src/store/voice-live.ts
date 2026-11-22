import { backendScopeKey } from '@hermes/shared'
import { atom } from 'nanostores'

import { fetchVoiceLiveStatus, type VoiceLiveStatus } from '@/lib/voice-live'
import { requestGatewayForActiveRoute } from '@/store/active-route-request'
import { activeGatewayConnectionId, activeGatewayProfileKey } from '@/store/gateway'

/**
 * `voice.voice_chat_mode` as the backend resolves it, plus whether GPT-Live can
 * actually start (an OpenAI key resolves on the gateway host). The composer
 * mounts the chained or the live conversation engine from this; refreshed with
 * the config snapshot so a Settings change applies to the next conversation.
 */
export const $voiceLiveStatus = atom<null | VoiceLiveStatus>(null)

// Bumped by every mode write: a fetch issued before the write reads the old
// mode, so it must neither be joined nor allowed to publish afterwards.
let writeEpoch = 0

let inflight: null | { key: string; promise: Promise<null | VoiceLiveStatus> } = null

// The config snapshot re-reads this on every refresh (session.info edge, draft
// mount, scope change). The resolved mode only changes when the config does, so
// a background caller inside this window reuses the answer instead of paying a
// round trip; a mode write always passes `force` and re-reads at once.
const VOICE_STATUS_MAX_AGE_MS = 30_000
let settledAt = 0
let settledKey = ''

// The single-flight slot is shared only within one (connection, profile, write)
// scope; a switch or a write strands the older fetch instead of joining it.
function voiceLiveScopeKey(): string {
  return `${backendScopeKey(activeGatewayConnectionId(), activeGatewayProfileKey())}::${writeEpoch}`
}

export async function refreshVoiceLiveStatus(force = false): Promise<null | VoiceLiveStatus> {
  const key = voiceLiveScopeKey()

  if (inflight?.key === key) {
    return inflight.promise
  }

  if (!force && settledKey === key && Date.now() - settledAt < VOICE_STATUS_MAX_AGE_MS) {
    return $voiceLiveStatus.get()
  }

  const flight = {
    key,
    promise: fetchVoiceLiveStatus()
      .then(status => {
        // A late answer for a previous scope still resolves for its caller but
        // must not overwrite the status the current profile/connection reads.
        if (voiceLiveScopeKey() === key) {
          $voiceLiveStatus.set(status)
          settledKey = key
          settledAt = Date.now()
        }

        return status
      })
      .finally(() => {
        if (inflight === flight) {
          inflight = null
        }
      })
  }

  inflight = flight

  return flight.promise
}

/** Selected mode. `chained` until the backend answers, or when the backend predates the mode. */
export function selectedVoiceChatMode(status: null | VoiceLiveStatus = $voiceLiveStatus.get()): 'chained' | 'gpt-live' {
  return status?.mode === 'gpt-live' ? 'gpt-live' : 'chained'
}

/**
 * Persist `voice.voice_chat_mode` on the live gateway (whichever profile/host
 * the app is talking to) and re-read the resolved status, so the menu shows
 * what the backend will actually mount next. Takes effect on the NEXT
 * conversation; an active one keeps its engine.
 *
 * Routed by (connection, profile), not by socket: `config.set` is
 * `@_profile_scoped`, so on the shared-primary route an unscoped write lands in
 * the LAUNCH profile's config.yaml, not the viewed one (#125969 class); and a
 * bare profile name would resolve through this machine's pool, missing a
 * profile served only by a remote gateway.
 */
export async function setVoiceChatMode(mode: 'chained' | 'gpt-live'): Promise<null | VoiceLiveStatus> {
  // The profile rides in the params too: on a backend shared by several
  // profiles an unscoped write edits its launch profile.
  const profile = activeGatewayProfileKey()
  await requestGatewayForActiveRoute(profile, 'config.set', { key: 'voice.voice_chat_mode', profile, value: mode })

  writeEpoch += 1

  return refreshVoiceLiveStatus(true)
}
