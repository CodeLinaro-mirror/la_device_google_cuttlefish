#!/usr/bin/env python3
# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Cuttlefish VoIP AudioModeSession Routing & Preemption E2E Test Suite."""

import audio_test_base
from mobly import asserts
from mobly import test_runner

FOCUS_CALLBACK_TIMEOUT_SEC = audio_test_base.FOCUS_CALLBACK_TIMEOUT_SEC
ON_AUDIO_FOCUS_CHANGE_EVENT = audio_test_base.ON_AUDIO_FOCUS_CHANGE_EVENT

MEDIA_CLIENT_ID = 'media_client'
VOIP_SESSION_ID = 'whatsapp_voip'
CELLULAR_SESSION_ID = 'cellular_call'
VOIP_STREAM_ID = 'voip_stream'
MEDIA_STREAM_ID = 'media_stream'

USAGE_MEDIA = audio_test_base.USAGE_MEDIA
USAGE_VOICE_COMMUNICATION = audio_test_base.USAGE_VOICE_COMMUNICATION

CONTENT_TYPE_SPEECH = audio_test_base.CONTENT_TYPE_SPEECH
CONTENT_TYPE_MUSIC = audio_test_base.CONTENT_TYPE_MUSIC

AUDIOFOCUS_GAIN = audio_test_base.AUDIOFOCUS_GAIN
AUDIOFOCUS_LOSS_TRANSIENT = audio_test_base.AUDIOFOCUS_LOSS_TRANSIENT
AUDIOFOCUS_REQUEST_GRANTED = audio_test_base.AUDIOFOCUS_REQUEST_GRANTED

MODE_NORMAL = audio_test_base.MODE_NORMAL
MODE_IN_CALL = audio_test_base.MODE_IN_CALL
MODE_IN_COMMUNICATION = audio_test_base.MODE_IN_COMMUNICATION

TYPE_BUILTIN_EARPIECE = audio_test_base.TYPE_BUILTIN_EARPIECE
TYPE_BUILTIN_SPEAKER = audio_test_base.TYPE_BUILTIN_SPEAKER

SPEAKER_DEVICE_TOKEN = audio_test_base.SPEAKER_DEVICE_TOKEN
EARPIECE_DEVICE_TOKEN = audio_test_base.EARPIECE_DEVICE_TOKEN
SPEAKER_ADDRESS = audio_test_base.SPEAKER_ADDRESS
EARPIECE_ADDRESS = audio_test_base.EARPIECE_ADDRESS
SPEAKER_ALSA_DEV = audio_test_base.SPEAKER_ALSA_DEV
EARPIECE_ALSA_DEV = audio_test_base.EARPIECE_ALSA_DEV


class CuttlefishVoipModeSessionTest(audio_test_base.CuttlefishAudioTestBase):
  """E2E VoIP call routing and preemption tests using AudioModeSession."""

  LOGCAT_SERVICE_NAME = 'audio_mode_session_logcat'

  def setup_class(self):
    super().setup_class()
    asserts.abort_class_if(
        not self.dut.audio.isModeSessionSupported(),
        'AudioModeSession (Flags.fusedTelecomRouteApi) is disabled on device.',
    )

  def test_dumpsys_format_contract(self):
    """Verifies live dumpsys format contracts for AudioService, APM, AF, and ALSA."""
    focus_res = self.dut.audio.requestAudioFocus(
        MEDIA_CLIENT_ID, USAGE_MEDIA, CONTENT_TYPE_MUSIC, AUDIOFOCUS_GAIN
    )
    asserts.assert_equal(focus_res, AUDIOFOCUS_REQUEST_GRANTED)

    focus_stack = self.inspector.get_focus_stack()
    asserts.assert_in(
        f'CfAudioTest[{MEDIA_CLIENT_ID}]',
        focus_stack,
        'Expected MediaFocusControl active focus stack format to contain'
        f' CfAudioTest[{MEDIA_CLIENT_ID}], got: {focus_stack!r}',
    )

    routed_type = self.dut.audio.createModeSession(
        VOIP_SESSION_ID, MODE_IN_COMMUNICATION, False
    )
    asserts.assert_equal(routed_type, TYPE_BUILTIN_EARPIECE)

    self.dut.audio.startPlayback(
        VOIP_STREAM_ID, USAGE_VOICE_COMMUNICATION, CONTENT_TYPE_SPEECH
    )

    snap = self.inspector.snapshot()
    asserts.assert_equal(snap.requested_mode, 'MODE_IN_COMMUNICATION')
    asserts.assert_equal(snap.actual_mode, 'MODE_IN_COMMUNICATION')
    asserts.assert_in(
        'USAGE_VOICE_COMMUNICATION', snap.started_playback_usages
    )
    asserts.assert_true(
        any(
            not s.paused
            and s.has_focus
            and s.mode == 'MODE_IN_COMMUNICATION'
            and EARPIECE_ADDRESS in s.target_route
            for s in snap.mode_sessions
        ),
        'Expected AudioService dumpsys AudioModeSession block with'
        f' MODE_IN_COMMUNICATION and {EARPIECE_ADDRESS}, got'
        f' {snap.mode_sessions}',
    )

    asserts.assert_in('STRATEGY_PHONE', snap.strategies)
    asserts.assert_in('STRATEGY_MEDIA', snap.strategies)
    phone_strat = snap.strategies['STRATEGY_PHONE']
    asserts.assert_in(EARPIECE_DEVICE_TOKEN, phone_strat.device_types)
    asserts.assert_equal(phone_strat.address, EARPIECE_ADDRESS)

    asserts.assert_true(
        bool(snap.output_threads),
        'Expected AudioFlinger dumpsys to report at least one OutputThread',
    )
    asserts.assert_true(
        self.inspector.has_alsa_playback_device(SPEAKER_ALSA_DEV),
        f'Expected ALSA playback device pcm{SPEAKER_ALSA_DEV}p to exist',
    )
    asserts.assert_true(
        self.inspector.has_alsa_playback_device(EARPIECE_ALSA_DEV),
        f'Expected ALSA playback device pcm{EARPIECE_ALSA_DEV}p to exist',
    )
    asserts.assert_in(SPEAKER_ALSA_DEV, snap.alsa_playback_running)
    asserts.assert_in(EARPIECE_ALSA_DEV, snap.alsa_playback_running)

  def test_voip_mode_session_default_voice_vs_video_routing(self):
    """Verifies AudioModeSession default routing for voice (Earpiece) vs video (Speaker)."""
    # 1. Voice-only VoIP session (isDisplayActiveUseCase=False) -> defaults to Earpiece
    routed_type = self.dut.audio.createModeSession(
        VOIP_SESSION_ID, MODE_IN_COMMUNICATION, False
    )
    asserts.assert_equal(
        routed_type,
        TYPE_BUILTIN_EARPIECE,
        'Expected voice VoIP AudioModeSession (isDisplayActiveUseCase=False) to'
        f' default to TYPE_BUILTIN_EARPIECE ({TYPE_BUILTIN_EARPIECE}), got'
        f' {routed_type}',
    )

    self.dut.audio.startPlayback(
        VOIP_STREAM_ID, USAGE_VOICE_COMMUNICATION, CONTENT_TYPE_SPEECH
    )
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

    # 2. Switch session to video/display-active use case (isDisplayActiveUseCase=True)
    # -> default route transitions from Earpiece to Speaker.
    updated_type = self.dut.audio.setSessionDisplayActiveUseCase(
        VOIP_SESSION_ID, True
    )
    asserts.assert_equal(
        updated_type,
        TYPE_BUILTIN_SPEAKER,
        'Expected video VoIP AudioModeSession (isDisplayActiveUseCase=True) to'
        f' route to TYPE_BUILTIN_SPEAKER ({TYPE_BUILTIN_SPEAKER}), got'
        f' {updated_type}',
    )
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=SPEAKER_DEVICE_TOKEN,
        expected_address=SPEAKER_ADDRESS,
        expected_alsa_dev=SPEAKER_ALSA_DEV,
        inactive_alsa_dev=EARPIECE_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

  def test_voip_mode_session_mid_call_route_switching(self):
    """Verifies mid-call Earpiece <-> Speaker switching via AudioModeSession.setRequestedRoute."""
    routed_type = self.dut.audio.createModeSession(
        VOIP_SESSION_ID, MODE_IN_COMMUNICATION, False
    )
    asserts.assert_equal(routed_type, TYPE_BUILTIN_EARPIECE)

    available_types = self.dut.audio.getSessionAvailableRouteTypes(
        VOIP_SESSION_ID
    )
    asserts.assert_in(TYPE_BUILTIN_EARPIECE, available_types)
    asserts.assert_in(TYPE_BUILTIN_SPEAKER, available_types)

    self.dut.audio.startPlayback(
        VOIP_STREAM_ID, USAGE_VOICE_COMMUNICATION, CONTENT_TYPE_SPEECH
    )

    # Initial state: Earpiece (CARD_0_DEV_1 / pcm1p)
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

    # Switch mid-call to Speaker (CARD_0_DEV_0 / pcm0p)
    speaker_type = self.dut.audio.requestSessionRouteByType(
        VOIP_SESSION_ID, TYPE_BUILTIN_SPEAKER
    )
    asserts.assert_equal(speaker_type, TYPE_BUILTIN_SPEAKER)
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=SPEAKER_DEVICE_TOKEN,
        expected_address=SPEAKER_ADDRESS,
        expected_alsa_dev=SPEAKER_ALSA_DEV,
        inactive_alsa_dev=EARPIECE_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

    # Switch mid-call back to Earpiece (CARD_0_DEV_1 / pcm1p)
    earpiece_type = self.dut.audio.requestSessionRouteByType(
        VOIP_SESSION_ID, TYPE_BUILTIN_EARPIECE
    )
    asserts.assert_equal(earpiece_type, TYPE_BUILTIN_EARPIECE)
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

  def test_voip_mode_session_preempts_and_restores_media_playback(self):
    """Verifies AudioModeSession preempts media focus/routing and restores it on close."""
    media_handler = self.dut.audio.registerFocusChangeListener(MEDIA_CLIENT_ID)

    # 1. Start Media playback with AUDIOFOCUS_GAIN on Speaker (CARD_0_DEV_0 / pcm0p)
    focus_res = self.dut.audio.requestAudioFocus(
        MEDIA_CLIENT_ID, USAGE_MEDIA, CONTENT_TYPE_MUSIC, AUDIOFOCUS_GAIN
    )
    asserts.assert_equal(focus_res, AUDIOFOCUS_REQUEST_GRANTED)

    self.dut.audio.startPlayback(
        MEDIA_STREAM_ID, USAGE_MEDIA, CONTENT_TYPE_MUSIC
    )
    self._assert_5_layer_route(
        strategy_name='STRATEGY_MEDIA',
        expected_device_token=SPEAKER_DEVICE_TOKEN,
        expected_address=SPEAKER_ADDRESS,
        expected_alsa_dev=SPEAKER_ALSA_DEV,
        inactive_alsa_dev=EARPIECE_ALSA_DEV,
        expected_mode='MODE_NORMAL',
    )

    # 2. Start VoIP AudioModeSession -> automatically acquires call focus and locks focus
    routed_type = self.dut.audio.createModeSession(
        VOIP_SESSION_ID, MODE_IN_COMMUNICATION, False
    )
    asserts.assert_equal(routed_type, TYPE_BUILTIN_EARPIECE)

    loss_event = media_handler.waitAndGet(
        ON_AUDIO_FOCUS_CHANGE_EVENT, timeout=FOCUS_CALLBACK_TIMEOUT_SEC
    )
    asserts.assert_equal(loss_event.data['clientId'], MEDIA_CLIENT_ID)
    asserts.assert_equal(
        loss_event.data['focusChange'], AUDIOFOCUS_LOSS_TRANSIENT
    )

    # Media app pauses on AUDIOFOCUS_LOSS_TRANSIENT while VoIP streams on Earpiece
    self.dut.audio.stopPlayback(MEDIA_STREAM_ID)
    self.dut.audio.startPlayback(
        VOIP_STREAM_ID, USAGE_VOICE_COMMUNICATION, CONTENT_TYPE_SPEECH
    )
    voip_snap = self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )
    asserts.assert_true(
        any(not s.paused and s.has_focus for s in voip_snap.mode_sessions),
        f'Expected live AudioModeSession to hold audio focus, got'
        f' {voip_snap.mode_sessions}',
    )

    # 3. End VoIP AudioModeSession -> Media automatically receives AUDIOFOCUS_GAIN & resumes Speaker
    self.dut.audio.stopPlayback(VOIP_STREAM_ID)
    self.dut.audio.closeModeSession(VOIP_SESSION_ID)

    gain_event = media_handler.waitAndGet(
        ON_AUDIO_FOCUS_CHANGE_EVENT, timeout=FOCUS_CALLBACK_TIMEOUT_SEC
    )
    asserts.assert_equal(gain_event.data['clientId'], MEDIA_CLIENT_ID)
    asserts.assert_equal(gain_event.data['focusChange'], AUDIOFOCUS_GAIN)

    self.dut.audio.startPlayback(
        MEDIA_STREAM_ID, USAGE_MEDIA, CONTENT_TYPE_MUSIC
    )
    self._assert_5_layer_route(
        strategy_name='STRATEGY_MEDIA',
        expected_device_token=SPEAKER_DEVICE_TOKEN,
        expected_address=SPEAKER_ADDRESS,
        expected_alsa_dev=SPEAKER_ALSA_DEV,
        inactive_alsa_dev=EARPIECE_ALSA_DEV,
        expected_mode='MODE_NORMAL',
    )

  def test_voip_mode_session_preempted_by_cellular_call_and_restored(self):
    """Verifies MODE_IN_CALL preempts a MODE_IN_COMMUNICATION session and restores on close."""
    # 1. Start WhatsApp-style VoIP session (MODE_IN_COMMUNICATION) and explicitly set
    # requestedRoute=Earpiece so AudioService persists and restores this route across preemption.
    init_voip_type = self.dut.audio.createModeSession(
        VOIP_SESSION_ID, MODE_IN_COMMUNICATION, False
    )
    asserts.assert_equal(init_voip_type, TYPE_BUILTIN_EARPIECE)
    asserts.assert_equal(
        self.dut.audio.requestSessionRouteByType(
            VOIP_SESSION_ID, TYPE_BUILTIN_EARPIECE
        ),
        TYPE_BUILTIN_EARPIECE,
    )

    self.dut.audio.startPlayback(
        VOIP_STREAM_ID, USAGE_VOICE_COMMUNICATION, CONTENT_TYPE_SPEECH
    )
    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )

    # 2. Incoming/active cellular call session (MODE_IN_CALL) starts and routes to Speaker.
    # Keep VOIP_STREAM_ID (USAGE_VOICE_COMMUNICATION -> STRATEGY_PHONE) playing to exercise
    # live AudioFlinger/HAL re-routing while MODE_IN_CALL owns STRATEGY_PHONE routing.
    init_call_type = self.dut.audio.createModeSession(
        CELLULAR_SESSION_ID, MODE_IN_CALL, True
    )
    asserts.assert_equal(init_call_type, TYPE_BUILTIN_SPEAKER)
    asserts.assert_true(
        self.dut.audio.awaitSessionPaused(VOIP_SESSION_ID),
        'Expected VoIP AudioModeSession to pause when preempted by MODE_IN_CALL session',
    )

    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=SPEAKER_DEVICE_TOKEN,
        expected_address=SPEAKER_ADDRESS,
        expected_alsa_dev=SPEAKER_ALSA_DEV,
        inactive_alsa_dev=EARPIECE_ALSA_DEV,
        expected_mode='MODE_IN_CALL',
    )

    # 3. Close cellular call session -> VoIP session automatically resumes and restores Earpiece
    self.dut.audio.closeModeSession(CELLULAR_SESSION_ID)
    restored_type = self.dut.audio.awaitSessionResumedAndRouted(VOIP_SESSION_ID)
    asserts.assert_equal(
        restored_type,
        TYPE_BUILTIN_EARPIECE,
        'Expected resumed VoIP AudioModeSession to restore'
        f' TYPE_BUILTIN_EARPIECE ({TYPE_BUILTIN_EARPIECE}), got {restored_type}',
    )

    self._assert_5_layer_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token=EARPIECE_DEVICE_TOKEN,
        expected_address=EARPIECE_ADDRESS,
        expected_alsa_dev=EARPIECE_ALSA_DEV,
        inactive_alsa_dev=SPEAKER_ALSA_DEV,
        expected_mode='MODE_IN_COMMUNICATION',
    )


if __name__ == '__main__':
  test_runner.main()
