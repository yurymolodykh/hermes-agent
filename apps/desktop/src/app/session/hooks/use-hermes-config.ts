import { type MutableRefObject, useCallback, useRef, useState } from 'react'

import { setTerminalFontFamilyFromConfig } from '@/app/right-sidebar/terminal/terminal-font'
import { getHermesConfig, getHermesConfigDefaults } from '@/hermes'
import { BUILTIN_PERSONALITIES, normalizePersonalityValue, personalityNamesFromConfig } from '@/lib/chat-runtime'
import { composerServiceTier } from '@/lib/model-status-label'
import { normalize } from '@/lib/text'
import { $busyInputConfig, busyInputOwnerKey, normalizeBusyInputMode } from '@/store/busy-input-mode'
import { setDisplayTimestampsFromConfig } from '@/store/display-timestamps'
import { $activeGatewayProfile } from '@/store/profile'
import { localShowReasoning, setShowReasoningFromConfig } from '@/store/reasoning-disclosure'
import { $connection } from '@/store/session'
import {
  getComposerSelectionGeneration,
  getCurrentModelSource,
  setAvailablePersonalities,
  setCurrentFastMode,
  setCurrentPersonality,
  setCurrentReasoningEffort,
  setCurrentServiceTier,
  setDefaultReasoningEffort,
  setIntroPersonality
} from '@/store/session'
import { setShowToolActivityFromConfig } from '@/store/tool-activity'
import { refreshVoiceLiveStatus } from '@/store/voice-live'
import {
  applyAutoSpeakFromConfig,
  applyBargeInEnabledFromConfig,
  applyBargeInThresholdFromConfig,
  applyThinkingSoundFromConfig,
  applyVoiceSilenceMsFromConfig,
  applyVoiceStopPhraseFromConfig
} from '@/store/voice-prefs'
import { setChatFontFamilyFromConfig } from '@/themes/chat-font'

const DEFAULT_VOICE_SECONDS = 120

function recordingLimit(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : DEFAULT_VOICE_SECONDS
}

/** config.yaml hands back whatever the user wrote — `reasoning_effort: false`
 *  (or `off`/`no`, which YAML also parses to boolean false) means thinking
 *  disabled, and a bare boolean must not throw on `.trim()`. */
function normalizeConfigEffort(value: unknown): string {
  if (value === false) {
    return 'none'
  }

  if (typeof value !== 'string') {
    return ''
  }

  const effort = normalize(value)

  return effort === 'false' || effort === 'disabled' ? 'none' : effort
}

interface HermesConfigOptions {
  activeSessionIdRef: MutableRefObject<string | null>
}

// One shared in-flight config+defaults pair per (connection, profile). Bursting
// background callers join it; the slot clears as soon as it settles so the next
// caller reads fresh values. `force` callers (scope change, explicit reseed) never
// join — they carry an intent that a response already in flight would answer from
// the state before their change.
let configReadInflight: null | { key: string; promise: Promise<[Awaited<ReturnType<typeof getHermesConfig>>, Awaited<ReturnType<typeof getHermesConfigDefaults>>]> } = null

function readHermesConfigOnce(
  connection: ReturnType<typeof $connection.get>,
  profile: string,
  force: boolean
): Promise<[Awaited<ReturnType<typeof getHermesConfig>>, Awaited<ReturnType<typeof getHermesConfigDefaults>>]> {
  const key = `${connection?.connectionId ?? connection?.baseUrl ?? ''}::${profile}`

  if (!force && configReadInflight?.key === key) {
    return configReadInflight.promise
  }

  const flight = {
    key,
    promise: Promise.all([getHermesConfig(), getHermesConfigDefaults().catch(() => ({}) as never)]) as Promise<
      [Awaited<ReturnType<typeof getHermesConfig>>, Awaited<ReturnType<typeof getHermesConfigDefaults>>]
    >
  }

  // A forced read supersedes the slot: it is the newest question, and a later
  // background caller should join it rather than the read it replaced.
  configReadInflight = flight
  void flight.promise.catch(() => undefined).then(() => {
    if (configReadInflight === flight) {
      configReadInflight = null
    }
  })

  return flight.promise
}

export function useHermesConfig({ activeSessionIdRef }: HermesConfigOptions) {
  const [voiceMaxRecordingSeconds, setVoiceMaxRecordingSeconds] = useState(DEFAULT_VOICE_SECONDS)
  const [sttEnabled, setSttEnabled] = useState(true)
  const profileRefreshEpochRef = useRef(0)

  const refreshHermesConfig = useCallback(
    async (force = false, shouldPublish: () => boolean = () => true) => {
      if (force) {
        profileRefreshEpochRef.current += 1
      }

      const profileRefreshEpoch = profileRefreshEpochRef.current
      const selectionGeneration = getComposerSelectionGeneration()
      const connection = $connection.get()
      const profile = $activeGatewayProfile.get()

      try {
        // Two REST reads per refresh (config + defaults), and the callers arrive in
        // bursts — a session.info edge, a draft mount and a scope change land in the
        // same second. Measured on an idle Webapp tab they showed up as 5–7
        // /api/config requests inside one second. They ask the same question of the
        // same backend, so share one in-flight pair instead of stacking them; a
        // caller that arrives after it settled starts a fresh read, which keeps
        // "save settings, see the result" immediate.
        const [config, defaults] = await readHermesConfigOnce(connection, profile, force)

        const canPublish = () => profileRefreshEpochRef.current === profileRefreshEpoch && shouldPublish()

        if (!canPublish()) {
          return
        }

        const personality = normalizePersonalityValue(
          typeof config.display?.personality === 'string' ? config.display.personality : ''
        )

        if (!canPublish()) {
          return
        }

        setIntroPersonality(personality)
        // Active sessions keep their per-session value; standalone falls back to config.
        setCurrentPersonality(prev => (activeSessionIdRef.current ? prev || personality : personality))
        setAvailablePersonalities([
          ...new Set([
            'none',
            ...BUILTIN_PERSONALITIES,
            ...personalityNamesFromConfig(defaults),
            ...personalityNamesFromConfig(config)
          ])
        ])

        const reasoning = normalizeConfigEffort(config.agent?.reasoning_effort)
        const tier = composerServiceTier(config.agent?.service_tier)

        // Publish the profile default regardless of whether the composer is
        // reseeded below: picker rows and preset application resolve "the
        // default" from here, so a manual model pick must not leave them
        // rendering/applying Hermes' built-in medium over the user's config.
        if (!canPublish()) {
          return
        }

        setDefaultReasoningEffort(reasoning)

        const shouldSeedComposer =
          !activeSessionIdRef.current &&
          getComposerSelectionGeneration() === selectionGeneration &&
          (force || getCurrentModelSource() !== 'manual')

        if (shouldSeedComposer) {
          if (!canPublish()) {
            return
          }

          setCurrentReasoningEffort(reasoning)
          setCurrentFastMode(tier === 'priority' || tier === 'ultrafast')
          setCurrentServiceTier(tier)
        }

        if (!canPublish()) {
          return
        }

        if (!canPublish()) {
          return
        }

        setVoiceMaxRecordingSeconds(recordingLimit(config.voice?.max_recording_seconds))
        setSttEnabled(config.stt?.enabled !== false)

        if (!canPublish()) {
          return
        }

        if ($connection.get() === connection && $activeGatewayProfile.get() === profile) {
          $busyInputConfig.set({
            owner: busyInputOwnerKey(connection?.connectionId, profile),
            connection,
            mode: normalizeBusyInputMode(config.display?.busy_input_mode)
          })
        }

        setDisplayTimestampsFromConfig(config.display?.timestamps)
        setShowReasoningFromConfig(localShowReasoning(config.display))
        setShowToolActivityFromConfig(config.display?.tool_progress)
        setTerminalFontFamilyFromConfig(config.terminal?.font_family)
        setChatFontFamilyFromConfig(config.desktop?.font_family)

        if (!canPublish()) {
          return
        }

        applyAutoSpeakFromConfig(config)
        applyVoiceStopPhraseFromConfig(config, defaults)
        applyBargeInEnabledFromConfig(config)
        applyBargeInThresholdFromConfig(config)
        applyThinkingSoundFromConfig(config)
        applyVoiceSilenceMsFromConfig(config, defaults)
        // Resolved server-side (mode + whether a key resolves); non-critical.
        void refreshVoiceLiveStatus().catch(() => undefined)
      } catch {
        // Config is nice-to-have; chat still works without it.
      }
    },
    [activeSessionIdRef]
  )

  return { refreshHermesConfig, sttEnabled, voiceMaxRecordingSeconds }
}
