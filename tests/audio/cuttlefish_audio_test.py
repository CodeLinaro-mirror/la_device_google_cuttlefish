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
"""Cuttlefish Audio Focus and E2E Test Suite."""

import os

from mobly import asserts
from mobly import base_test
from mobly import test_runner
from mobly.controllers import android_device
from mobly.controllers.android_device_lib.services import logcat

SNIPPET_PACKAGE = 'com.android.cuttlefish.audiotest'
AUDIO_FOCUS_LOGCAT_TAGS = (
    'AudioFocusSnippet:V AudioService:V MediaFocusControl:V'
)
FOCUS_CALLBACK_TIMEOUT_SEC = 5
ON_AUDIO_FOCUS_CHANGE_EVENT = 'onAudioFocusChange'
MEDIA_CLIENT_ID = 'media_client'
ALARM_CLIENT_ID = 'alarm_client'

# AudioAttributes.Usage constants
USAGE_MEDIA = 1
USAGE_VOICE_COMMUNICATION = 2
USAGE_ALARM = 4
USAGE_NOTIFICATION = 5
USAGE_ASSISTANCE_NAVIGATION_GUIDANCE = 12
USAGE_GAME = 14

# AudioAttributes.ContentType constants
CONTENT_TYPE_SPEECH = 1
CONTENT_TYPE_MUSIC = 2
CONTENT_TYPE_SONIFICATION = 4

# AudioManager focus gain / loss constants
AUDIOFOCUS_GAIN = 1
AUDIOFOCUS_GAIN_TRANSIENT = 2
AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK = 3
AUDIOFOCUS_LOSS_TRANSIENT = -2

# AudioManager focus request result constants
AUDIOFOCUS_REQUEST_FAILED = 0
AUDIOFOCUS_REQUEST_GRANTED = 1
AUDIOFOCUS_REQUEST_DELAYED = 2

# Declarative Audio Focus use-case matrix:
# (use_case_name, usage, content_type, focus_gain, expected_result)
AUDIO_FOCUS_USE_CASES = [
    (
        'media',
        USAGE_MEDIA,
        CONTENT_TYPE_MUSIC,
        AUDIOFOCUS_GAIN,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
    (
        'voice_communication',
        USAGE_VOICE_COMMUNICATION,
        CONTENT_TYPE_SPEECH,
        AUDIOFOCUS_GAIN_TRANSIENT,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
    (
        'navigation',
        USAGE_ASSISTANCE_NAVIGATION_GUIDANCE,
        CONTENT_TYPE_SPEECH,
        AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
    (
        'alarm',
        USAGE_ALARM,
        CONTENT_TYPE_SONIFICATION,
        AUDIOFOCUS_GAIN_TRANSIENT,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
    (
        'notification',
        USAGE_NOTIFICATION,
        CONTENT_TYPE_SONIFICATION,
        AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
    (
        'game',
        USAGE_GAME,
        CONTENT_TYPE_SONIFICATION,
        AUDIOFOCUS_GAIN,
        AUDIOFOCUS_REQUEST_GRANTED,
    ),
]


class CuttlefishAudioFocusTest(base_test.BaseTestClass):
  """Data-driven Audio Focus validation suite for Cuttlefish."""

  # Focus tests only require `dumpsys audio` + filtered focus logcat.
  # Future streaming/routing test classes can extend this map with:
  #   'dumpsys_audio_flinger': 'dumpsys media.audio_flinger',
  #   'dumpsys_audio_policy': 'dumpsys media.audio_policy',
  DUMPSYS_TARGETS = {
      'dumpsys_audio': 'dumpsys audio',
  }

  def setup_class(self):
    self.ads = self.register_controller(android_device)
    self.dut = self.ads[0]
    self.dut.services.register(
        'audio_focus_logcat',
        logcat.Logcat,
        configs=logcat.Config(logcat_params=f'-s {AUDIO_FOCUS_LOGCAT_TAGS}'),
    )
    self.dut.load_snippet('audio', SNIPPET_PACKAGE)

  def setup_test(self):
    self.dut.audio.abandonAllAudioFocus()

  def teardown_test(self):
    self.dut.audio.abandonAllAudioFocus()

  def on_fail(self, record):
    del record  # Unused; self.current_test_info scopes output files.
    self.dut.services.create_output_excerpts_all(self.current_test_info)
    for file_type, cmd in self.DUMPSYS_TARGETS.items():
      filename = self.dut.generate_filename(
          file_type, self.current_test_info, 'txt'
      )
      output_file = os.path.join(self.current_test_info.output_path, filename)
      content = self.dut.adb.shell(cmd).decode('utf-8', errors='replace')
      with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)

  def teardown_class(self):
    self.dut.unload_snippet('audio')

  def pre_run(self):
    self.generate_tests(
        test_logic=self._verify_audio_focus_use_case,
        name_func=lambda use_case, usage, content_type, focus_gain, expected_result: (
            f'test_audio_focus_{use_case}'
        ),
        arg_sets=AUDIO_FOCUS_USE_CASES,
    )

  def _get_active_focus_stack(self) -> str:
    dumpsys_audio = self.dut.adb.shell('dumpsys audio').decode(
        'utf-8', errors='replace'
    )
    header = 'Audio Focus stack entries (last is top of stack):'
    footer = ' Notify on duck:'
    start_idx = dumpsys_audio.find(header)
    asserts.assert_not_equal(
        start_idx,
        -1,
        f'Could not find {header!r} section in dumpsys audio',
    )
    end_idx = dumpsys_audio.find(footer, start_idx)
    asserts.assert_not_equal(
        end_idx,
        -1,
        f'Could not find {footer!r} boundary in dumpsys audio',
    )
    return dumpsys_audio[start_idx:end_idx]

  def _verify_audio_focus_use_case(
      self,
      use_case: str,
      usage: int,
      content_type: int,
      focus_gain: int,
      expected_result: int,
  ):
    self.dut.log.info(
        'Requesting AudioFocus for use_case=%s (usage=%d, content_type=%d,'
        ' focus_gain=%d)',
        use_case,
        usage,
        content_type,
        focus_gain,
    )
    result = self.dut.audio.requestAudioFocus(
        use_case, usage, content_type, focus_gain
    )
    asserts.assert_equal(
        result,
        expected_result,
        f'Expected focus result {expected_result} for {use_case}, got {result}',
    )

    active_stack = self._get_active_focus_stack()
    asserts.assert_in(
        SNIPPET_PACKAGE,
        active_stack,
        f'Expected {SNIPPET_PACKAGE} in active focus stack for {use_case}, got:'
        f' {active_stack}',
    )

    abandon_result = self.dut.audio.abandonAudioFocus(use_case)
    asserts.assert_equal(
        abandon_result,
        AUDIOFOCUS_REQUEST_GRANTED,
        f'Expected abandonAudioFocus to succeed for {use_case}, got'
        f' {abandon_result}',
    )

    post_abandon_stack = self._get_active_focus_stack()
    asserts.assert_not_in(
        SNIPPET_PACKAGE,
        post_abandon_stack,
        f'Expected {SNIPPET_PACKAGE} to be removed from active focus stack'
        f' after abandon for {use_case}, got: {post_abandon_stack}',
    )

  def test_audio_focus_change_listener_callback(self):
    handler = self.dut.audio.registerFocusChangeListener(MEDIA_CLIENT_ID)

    media_res = self.dut.audio.requestAudioFocus(
        MEDIA_CLIENT_ID, USAGE_MEDIA, CONTENT_TYPE_MUSIC, AUDIOFOCUS_GAIN
    )
    asserts.assert_equal(media_res, AUDIOFOCUS_REQUEST_GRANTED)

    alarm_res = self.dut.audio.requestAudioFocus(
        ALARM_CLIENT_ID,
        USAGE_ALARM,
        CONTENT_TYPE_SONIFICATION,
        AUDIOFOCUS_GAIN_TRANSIENT,
    )
    asserts.assert_equal(alarm_res, AUDIOFOCUS_REQUEST_GRANTED)

    loss_event = handler.waitAndGet(
        ON_AUDIO_FOCUS_CHANGE_EVENT, timeout=FOCUS_CALLBACK_TIMEOUT_SEC
    )
    asserts.assert_equal(loss_event.data['clientId'], MEDIA_CLIENT_ID)
    asserts.assert_equal(
        loss_event.data['focusChange'], AUDIOFOCUS_LOSS_TRANSIENT
    )

    abandon_res = self.dut.audio.abandonAudioFocus(ALARM_CLIENT_ID)
    asserts.assert_equal(abandon_res, AUDIOFOCUS_REQUEST_GRANTED)

    regain_event = handler.waitAndGet(
        ON_AUDIO_FOCUS_CHANGE_EVENT, timeout=FOCUS_CALLBACK_TIMEOUT_SEC
    )
    asserts.assert_equal(regain_event.data['clientId'], MEDIA_CLIENT_ID)
    asserts.assert_equal(regain_event.data['focusChange'], AUDIOFOCUS_GAIN)

    self.dut.audio.unregisterFocusChangeListener(MEDIA_CLIENT_ID)
    self.dut.audio.abandonAudioFocus(MEDIA_CLIENT_ID)


if __name__ == '__main__':
  test_runner.main()
