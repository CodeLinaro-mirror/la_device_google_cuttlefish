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
"""Unit tests for the 5-layer Cuttlefish audio stack inspector."""

import dataclasses
import unittest
from unittest import mock

import audio_stack_inspector
from frameworks.base.core.proto.android.media import audioattributes_pb2
from frameworks.base.core.proto.android.server import audioservice_pb2

# AudioDeviceAttributes.ROLE_OUTPUT and AudioDeviceInfo.TYPE_BUILTIN_EARPIECE.
_ROLE_OUTPUT = 2
_ROLE_INPUT = 1
_TYPE_BUILTIN_EARPIECE = 1
_TYPE_BLUETOOTH_SCO = 7
_TYPE_BLUETOOTH_A2DP = 8
_EARPIECE_DEVICE_STR = 'DeviceIdentity(role=2, type=1, address=CARD_0_DEV_1)'
_EARPIECE_ROUTE_STR = f'Route(output={_EARPIECE_DEVICE_STR}, input=None)'


SAMPLE_DUMPSYS_AUDIO = """\
Audio Focus stack entries (last is top of stack):
  source:android.os.BinderProxy@1234 -- pack: com.android.cuttlefish.audiotest

## Audio mode:
  - Requested mode = MODE_IN_COMMUNICATION
  - Actual mode = MODE_IN_COMMUNICATION

  Computed Preferred communication device: AudioDeviceAttributes: role:output type:builtin_earpiece addr:CARD_0_DEV_1
  Applied Preferred communication device: AudioDeviceAttributes: role:output type:builtin_earpiece addr:CARD_0_DEV_1
  Active communication device: AudioDeviceAttributes: role:output type:builtin_earpiece addr:CARD_0_DEV_1

  AudioModeSession [7f3a1b]:
    package: com.android.cuttlefish.audiotest uid: 10142 pid: 4120
    mode: MODE_IN_COMMUNICATION (3)
    isDisplayActiveUseCase: false
    lifecycle: clientPaused=false serverPaused=false closed=false hasFocus=true isLive=true
    requestedRoute: Device(type: builtin_earpiece, addr: CARD_0_DEV_1)
    appliedRoute: Device(type: builtin_earpiece, addr: CARD_0_DEV_1)

players:
  AudioPlaybackConfiguration piid:15 deviceIds:[4] type:android.media.AudioTrack u/pid:10142/4120 state:started attr:AudioAttributes: usage=USAGE_VOICE_COMMUNICATION content=CONTENT_TYPE_SPEECH
"""

SAMPLE_DUMPSYS_AUDIO_POLICY = """\
Product Strategies:
  -STRATEGY_PHONE (id: 14)
   Selected Device: {AUDIO_DEVICE_OUT_EARPIECE, @:CARD_0_DEV_1}
  -STRATEGY_MEDIA (id: 15)
   Selected Device: {AUDIO_DEVICE_OUT_SPEAKER, @:CARD_0_DEV_0}

Audio Patches:
  Audio patch 0:
   owner uid 1041; handle 18; HAL handle 7
    [src 1] Mix Port ID: 21; I/O handle: 13;
    [sink 1] Device Port ID: 4; {AUDIO_DEVICE_OUT_EARPIECE, @:CARD_0_DEV_1}
"""

SAMPLE_DUMPSYS_AUDIO_FLINGER = """\
Output thread 0xb400007a10, name AudioOut_D, tid 512, type 0 (MIXER):
  I/O handle: 13
  Standby: no
  Sample rate: 48000 Hz
  Output devices: 0x1 (AUDIO_DEVICE_OUT_EARPIECE)
  Frames written: 24960
  1 Tracks of which 1 are active

Input thread 0xb400007b20, name AudioIn_15, tid 619, type 3 (RECORD):
  I/O handle: 21
  Standby: no
  Sample rate: 48000 Hz
  Input device: 0x80000004 (AUDIO_DEVICE_IN_BUILTIN_MIC)
  Audio source: 7 (AUDIO_SOURCE_VOICE_COMMUNICATION)

Historical Thread Log:
Output thread 0xb400007a00, name AudioOut_OLD, tid 400, type 0 (MIXER):
  I/O handle: 99
  Standby: no
  Output devices: 0x2 (AUDIO_DEVICE_OUT_SPEAKER)
  Frames written: 999999
  1 Tracks of which 1 are active
"""


class AudioStackInspectorTest(unittest.TestCase):
  """Tests for dumpsys parsers and 5-layer route convergence checks."""

  def test_parse_dumpsys_audio_happy_path_extracts_mode_and_session(self):
    parsed = audio_stack_inspector.parse_dumpsys_audio(SAMPLE_DUMPSYS_AUDIO)

    self.assertEqual(parsed['requested_mode'], 'MODE_IN_COMMUNICATION')
    self.assertEqual(parsed['actual_mode'], 'MODE_IN_COMMUNICATION')
    self.assertIn(
        'type:builtin_earpiece', str(parsed['active_comm_device'])
    )
    self.assertEqual(
        parsed['started_playback_usages'], ('USAGE_VOICE_COMMUNICATION',)
    )
    self.assertEqual(audioattributes_pb2.Usage.Name(17), 'CALL_ASSISTANT')
    self.assertEqual(audioattributes_pb2.Usage.Name(1004), 'SPEAKER_CLEANUP')
    sessions = parsed['mode_sessions']
    self.assertEqual(len(sessions), 1)
    self.assertEqual(
        sessions[0],
        audio_stack_inspector.AudioModeSessionInfo(
            short_id='7f3a1b',
            mode='MODE_IN_COMMUNICATION',
            is_display_active_use_case=False,
            client_paused=False,
            paused=False,
            closed=False,
            has_focus=True,
            requested_route='Device(type: builtin_earpiece, addr: CARD_0_DEV_1)',
            target_route='Device(type: builtin_earpiece, addr: CARD_0_DEV_1)',
        ),
    )

  def test_parse_dumpsys_audio_missing_anchor_raises_value_error(self):
    with self.assertRaisesRegex(
        ValueError, r"Missing section anchor '## Audio mode'.*unexpected dump"
    ):
      audio_stack_inspector.parse_dumpsys_audio('unexpected dump header')

  def test_parse_dumpsys_audio_policy_happy_path_extracts_strategies_and_patches(
      self,
  ):
    strategies, patches = audio_stack_inspector.parse_dumpsys_audio_policy(
        SAMPLE_DUMPSYS_AUDIO_POLICY
    )

    self.assertEqual(
        strategies['STRATEGY_PHONE'],
        audio_stack_inspector.StrategyRoute(
            name='STRATEGY_PHONE',
            device_types='AUDIO_DEVICE_OUT_EARPIECE',
            address='CARD_0_DEV_1',
        ),
    )
    self.assertEqual(
        strategies['STRATEGY_MEDIA'],
        audio_stack_inspector.StrategyRoute(
            name='STRATEGY_MEDIA',
            device_types='AUDIO_DEVICE_OUT_SPEAKER',
            address='CARD_0_DEV_0',
        ),
    )
    self.assertEqual(
        patches,
        (
            audio_stack_inspector.AudioPatchInfo(
                handle=18,
                src_mix_port_id=21,
                io_handle=13,
                sink_device_port_id=4,
                sink_device='AUDIO_DEVICE_OUT_EARPIECE, @:CARD_0_DEV_1',
            ),
        ),
    )

  def test_parse_dumpsys_audio_policy_missing_anchor_raises_value_error(self):
    with self.assertRaisesRegex(
        ValueError, r"Missing section anchor 'owner uid'.*no patches section"
    ):
      audio_stack_inspector.parse_dumpsys_audio_policy('no patches section')

  def test_parse_dumpsys_audio_flinger_happy_path_ignores_historical_log(self):
    output_threads, input_threads = (
        audio_stack_inspector.parse_dumpsys_audio_flinger(
            SAMPLE_DUMPSYS_AUDIO_FLINGER
        )
    )

    self.assertEqual(
        output_threads,
        {
            13: audio_stack_inspector.OutputThreadInfo(
                io_handle=13,
                standby=False,
                output_devices='0x1 (AUDIO_DEVICE_OUT_EARPIECE)',
                frames_written=24960,
                active_tracks=1,
            ),
        },
    )
    self.assertNotIn(99, output_threads)
    self.assertEqual(
        input_threads,
        {
            21: audio_stack_inspector.InputThreadInfo(
                io_handle=21,
                standby=False,
                input_device='0x80000004 (AUDIO_DEVICE_IN_BUILTIN_MIC)',
                audio_source='7 (AUDIO_SOURCE_VOICE_COMMUNICATION)',
            ),
        },
    )

  def test_parse_dumpsys_audio_flinger_missing_anchor_raises_value_error(self):
    with self.assertRaisesRegex(
        ValueError, r"Missing section anchor 'Output thread'.*empty flinger"
    ):
      audio_stack_inspector.parse_dumpsys_audio_flinger('empty flinger dump')

  def test_wait_for_route_converges_and_reports_failing_layers_on_timeout(self):
    dut = mock.MagicMock()
    inspector = audio_stack_inspector.AudioStackInspector(dut)
    layer1 = audio_stack_inspector.parse_dumpsys_audio(SAMPLE_DUMPSYS_AUDIO)
    strategies, patches = audio_stack_inspector.parse_dumpsys_audio_policy(
        SAMPLE_DUMPSYS_AUDIO_POLICY
    )
    out_threads, in_threads = audio_stack_inspector.parse_dumpsys_audio_flinger(
        SAMPLE_DUMPSYS_AUDIO_FLINGER
    )
    snap = audio_stack_inspector.AudioStackSnapshot(
        requested_mode=str(layer1['requested_mode']),
        actual_mode=str(layer1['actual_mode']),
        computed_preferred_comm_device=str(
            layer1['computed_preferred_comm_device']
        ),
        applied_preferred_comm_device=str(
            layer1['applied_preferred_comm_device']
        ),
        active_comm_device=str(layer1['active_comm_device']),
        started_playback_usages=tuple(layer1['started_playback_usages']),
        mode_sessions=tuple(layer1['mode_sessions']),
        strategies=strategies,
        patches=patches,
        output_threads=out_threads,
        input_threads=in_threads,
        alsa_playback_running={0: False, 1: True},
        alsa_capture_running={0: True},
    )
    inspector.snapshot = mock.MagicMock(return_value=snap)

    converged, returned_snap = inspector.wait_for_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_EARPIECE',
        expected_address='CARD_0_DEV_1',
        expected_alsa_dev=1,
        inactive_alsa_devs=(0,),
        expected_mode='MODE_IN_COMMUNICATION',
        timeout_sec=0.0,
    )

    self.assertTrue(converged)
    self.assertEqual(returned_snap, snap)

    timed_out_converged, timed_out_snap = inspector.wait_for_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_EARPIECE',
        expected_address='CARD_0_DEV_1',
        expected_alsa_dev=0,
        inactive_alsa_devs=(1,),
        expected_mode='MODE_IN_COMMUNICATION',
        timeout_sec=0.0,
    )
    mismatches = inspector.find_route_mismatches(
        timed_out_snap,
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_EARPIECE',
        expected_address='CARD_0_DEV_1',
        expected_alsa_dev=0,
        inactive_alsa_devs=(1,),
        expected_mode='MODE_IN_COMMUNICATION',
    )

    self.assertFalse(timed_out_converged)
    self.assertTrue(all('Layer 5 (ALSA)' in m for m in mismatches))

  def test_read_alsa_running_states_batches_probe_in_single_shell_call(self):
    dut = mock.MagicMock()
    dut.adb.shell.return_value = (
        b'===HAL_FDS===\n'
        b'lrwx------ 1audioserver 10 -> /dev/snd/pcmC0D1p\n'
        b'===PLAY_0===\n'
        b'closed\n'
        b'===PLAY_1===\n'
        b'state: RUNNING\n'
        b'===CAP_0===\n'
        b'state: RUNNING\n'
    )
    inspector = audio_stack_inspector.AudioStackInspector(dut)

    playback_running, capture_running = inspector.read_alsa_running_states()

    dut.adb.shell.assert_called_once()
    self.assertEqual(playback_running, {0: False, 1: True})
    self.assertEqual(capture_running, {0: True})

  def test_parse_focus_stack_slices_live_stack_and_rejects_malformed_dumps(self):
    sample_focus_dump = (
        'MediaFocusControl:\n'
        'Audio Focus stack entries (last is top of stack):\n'
        '  source:android.os.BinderProxy@1 -- client:'
        ' CfAudioTest[media_client]#1 -- gain: GAIN\n'
        'No external focus policy\n'
        ' Notify on duck: true\n'
        'focus commands as seen by MediaFocusControl:\n'
        '  requestAudioFocus() client: CfAudioTest[stale_historical_client]#9\n'
    )
    stack = audio_stack_inspector.parse_focus_stack(sample_focus_dump)
    self.assertIn('CfAudioTest[media_client]#1', stack)
    self.assertNotIn('CfAudioTest[stale_historical_client]#9', stack)

    with self.assertRaisesRegex(
        ValueError, r'Missing section anchor.*Audio Focus stack entries'
    ):
      audio_stack_inspector.parse_focus_stack('MediaFocusControl: empty')

    with self.assertRaisesRegex(
        ValueError, r'Missing expected focus stack terminator'
    ):
      audio_stack_inspector.parse_focus_stack(
          'Audio Focus stack entries (last is top of stack):\n'
          '  source:android.os.BinderProxy@1 -- client: CfAudioTest[media]#1\n'
      )

  def test_non_alsa_bluetooth_route_verifies_layers_and_inactive_alsa(self):
    dut = mock.MagicMock()
    inspector = audio_stack_inspector.AudioStackInspector(dut)
    bt_addr = 'F0:F1:F2:F3:F4:F5'
    bt_snap = audio_stack_inspector.AudioStackSnapshot(
        requested_mode='MODE_NORMAL',
        actual_mode='MODE_NORMAL',
        computed_preferred_comm_device='None',
        applied_preferred_comm_device='None',
        active_comm_device='None',
        started_playback_usages=('USAGE_MEDIA',),
        mode_sessions=(),
        strategies={
            'STRATEGY_MEDIA': audio_stack_inspector.StrategyRoute(
                name='STRATEGY_MEDIA',
                device_types='AUDIO_DEVICE_OUT_BLUETOOTH_A2DP',
                address=bt_addr,
            ),
        },
        patches=(
            audio_stack_inspector.AudioPatchInfo(
                handle=42,
                src_mix_port_id=31,
                io_handle=29,
                sink_device_port_id=12,
                sink_device=f'AUDIO_DEVICE_OUT_BLUETOOTH_A2DP, @:{bt_addr}',
            ),
        ),
        output_threads={
            29: audio_stack_inspector.OutputThreadInfo(
                io_handle=29,
                standby=False,
                output_devices='0x80 (AUDIO_DEVICE_OUT_BLUETOOTH_A2DP)',
                frames_written=48000,
                active_tracks=1,
            ),
        },
        input_threads={},
        alsa_playback_running={0: False, 1: False},
        alsa_capture_running={0: False},
    )
    mismatches = inspector.find_route_mismatches(
        bt_snap,
        strategy_name='STRATEGY_MEDIA',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_A2DP',
        expected_address=bt_addr,
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_NORMAL',
    )
    self.assertEqual(mismatches, [])

    leaking_alsa_snap = dataclasses.replace(
        bt_snap, alsa_playback_running={0: True, 1: False}
    )
    leak_mismatches = inspector.find_route_mismatches(
        leaking_alsa_snap,
        strategy_name='STRATEGY_MEDIA',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_A2DP',
        expected_address=bt_addr,
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_NORMAL',
    )
    self.assertEqual(len(leak_mismatches), 1)
    self.assertIn('expected pcm0p to be closed/inactive', leak_mismatches[0])

  def test_parse_dumpsys_audio_proto_and_focus_stack_proto_roundtrip(self):
    earpiece = audioservice_pb2.DeviceIdentityProto(
        role=_ROLE_OUTPUT,
        type=_TYPE_BUILTIN_EARPIECE,
        address='CARD_0_DEV_1',
    )
    dump_proto = audioservice_pb2.AudioServiceDumpProto()
    dump_proto.audio_mode.requested_mode = 'MODE_IN_COMMUNICATION'
    dump_proto.audio_mode.actual_mode = 'MODE_IN_COMMUNICATION'
    broker = dump_proto.device_broker
    broker.computed_preferred_communication_device.CopyFrom(earpiece)
    broker.applied_preferred_communication_device.CopyFrom(earpiece)
    broker.active_communication_device.CopyFrom(earpiece)
    session = dump_proto.device_broker.mode_sessions.add()
    session.short_id = '7f3a1b'
    session.mode = 'MODE_IN_COMMUNICATION'
    session.mode_int = 3
    session.is_display_active_use_case = False
    session.client_paused = False
    session.paused = False
    session.closed = False
    session.has_focus = True
    session.requested_route.output.CopyFrom(earpiece)
    session.target_route.output.CopyFrom(earpiece)

    player = dump_proto.playback_monitor.players.add()
    player.piid = 15
    player.player_state = (
        audioservice_pb2.AudioPlaybackConfigurationProto.PLAYER_STATE_STARTED
    )
    player.audio_attributes.usage = audioattributes_pb2.VOICE_COMMUNICATION

    req = dump_proto.media_focus_control.focus_stack.add()
    req.package_name = 'com.android.cuttlefish.audiotest'
    req.client_id = 'CfAudioTest[media_client]#1'
    req.focus_gain = 'GAIN'
    req.calling_uid = 10142
    req.audio_attributes.usage = audioattributes_pb2.MEDIA
    req.audio_attributes.content_type = audioattributes_pb2.MUSIC
    req.audio_attributes.flags = 0x1
    req.sdk_target = 35

    raw_bytes = dump_proto.SerializeToString()
    parsed = audio_stack_inspector.parse_dumpsys_audio_proto(raw_bytes)
    self.assertEqual(parsed['requested_mode'], 'MODE_IN_COMMUNICATION')
    self.assertEqual(parsed['actual_mode'], 'MODE_IN_COMMUNICATION')
    for key in (
        'computed_preferred_comm_device',
        'applied_preferred_comm_device',
        'active_comm_device',
    ):
      self.assertEqual(parsed[key], _EARPIECE_DEVICE_STR)
    self.assertEqual(
        parsed['started_playback_usages'], ('USAGE_VOICE_COMMUNICATION',)
    )
    self.assertEqual(audioattributes_pb2.Usage.Name(17), 'CALL_ASSISTANT')
    self.assertEqual(audioattributes_pb2.Usage.Name(1004), 'SPEAKER_CLEANUP')
    self.assertEqual(
        audioattributes_pb2.ContentType.Name(1997), 'ULTRASOUND'
    )
    self.assertEqual(
        parsed['mode_sessions'],
        (
            audio_stack_inspector.AudioModeSessionInfo(
                short_id='7f3a1b',
                mode='MODE_IN_COMMUNICATION',
                is_display_active_use_case=False,
                client_paused=False,
                paused=False,
                closed=False,
                has_focus=True,
                requested_route=_EARPIECE_ROUTE_STR,
                target_route=_EARPIECE_ROUTE_STR,
            ),
        ),
    )

    focus_stack = audio_stack_inspector.parse_focus_stack_proto(raw_bytes)
    self.assertIn('CfAudioTest[media_client]#1', focus_stack)
    self.assertIn('com.android.cuttlefish.audiotest', focus_stack)
    self.assertIn(
        'usage=USAGE_MEDIA content=CONTENT_TYPE_MUSIC flags=0x1', focus_stack
    )
    self.assertEqual(
        audio_stack_inspector._format_usage_proto(99999), 'USAGE_INVALID_99999'
    )
    self.assertEqual(
        audio_stack_inspector._format_content_type_proto(99999),
        'CONTENT_TYPE_INVALID_99999',
    )

    with self.assertRaisesRegex(
        ValueError, r"Can't find service: audio"
    ):
      audio_stack_inspector.parse_dumpsys_audio_proto(
          b"Can't find service: audio\n"
      )

    # Empty focus stack (ProtoOutputStream omits 0-byte media_focus_control sub-message)
    empty_focus_proto = audioservice_pb2.AudioServiceDumpProto()
    empty_focus_proto.audio_mode.requested_mode = 'MODE_NORMAL'
    empty_focus_proto.audio_mode.actual_mode = 'MODE_NORMAL'
    self.assertEqual(
        audio_stack_inspector.parse_dumpsys_audio_proto(
            empty_focus_proto.SerializeToString()
        )['applied_preferred_comm_device'],
        '',
    )
    self.assertEqual(
        audio_stack_inspector.parse_focus_stack_proto(
            empty_focus_proto.SerializeToString()
        ),
        '',
    )
    self.assertEqual(
        audio_stack_inspector.parse_focus_config_proto(
            empty_focus_proto.SerializeToString()
        ),
        audio_stack_inspector.FocusConfig(
            has_focus_policy=False, multi_audio_focus_enabled=False
        ),
    )

    dump_proto.media_focus_control.has_focus_policy = True
    dump_proto.media_focus_control.multi_audio_focus_enabled = True
    self.assertEqual(
        audio_stack_inspector.parse_focus_config_proto(
            dump_proto.SerializeToString()
        ),
        audio_stack_inspector.FocusConfig(
            has_focus_policy=True, multi_audio_focus_enabled=True
        ),
    )
    self.assertEqual(
        audio_stack_inspector.parse_focus_config(
            'Audio Focus stack entries (last is top of stack):\n'
            '  has focus policy:true\n'
            'Multi Audio Focus enabled :true\n'
        ),
        audio_stack_inspector.FocusConfig(
            has_focus_policy=True, multi_audio_focus_enabled=True
        ),
    )

  def test_parse_dumpsys_audio_post_route_selector_text_format(self):
    post_route_selector_dump = """\
## Audio mode:
  - Requested mode = MODE_IN_COMMUNICATION
  - Actual mode = MODE_IN_COMMUNICATION

  Computed Preferred communication device: AudioDeviceAttributes: role:output type:builtin_earpiece addr:CARD_0_DEV_1

  AudioModeSession [8a9b0c]:
    package: com.android.cuttlefish.audiotest uid: 10142 pid: 4120
    mode: MODE_IN_COMMUNICATION (3)
    isDisplayActiveUseCase: false
    lifecycle: clientPaused=false paused=false closed=false hasFocus=true isLive=true
    requestedRoute: Optional.empty
"""
    parsed = audio_stack_inspector.parse_dumpsys_audio(post_route_selector_dump)
    sessions = parsed['mode_sessions']
    self.assertEqual(len(sessions), 1)
    self.assertEqual(
        sessions[0],
        audio_stack_inspector.AudioModeSessionInfo(
            short_id='8a9b0c',
            mode='MODE_IN_COMMUNICATION',
            is_display_active_use_case=False,
            client_paused=False,
            paused=False,
            closed=False,
            has_focus=True,
            requested_route='Optional.empty',
            target_route=(
                'AudioDeviceAttributes: role:output type:builtin_earpiece'
                ' addr:CARD_0_DEV_1'
            ),
        ),
    )

    target_route_dump = """\
## Audio mode:
  - Requested mode = MODE_IN_COMMUNICATION
  - Actual mode = MODE_IN_COMMUNICATION

  AudioModeSession [8a9b0c]:
    package: com.android.cuttlefish.audiotest uid: 10142 pid: 4120
    mode: MODE_IN_COMMUNICATION (3)
    isDisplayActiveUseCase: false
    lifecycle: clientPaused=false paused=false closed=false hasFocus=true
    requestedRoute: Optional.empty
    targetRoute: Optional[Device(type: builtin_earpiece, addr: CARD_0_DEV_1)]
"""
    parsed_target = audio_stack_inspector.parse_dumpsys_audio(target_route_dump)
    self.assertEqual(
        parsed_target['mode_sessions'][0].target_route,
        'Device(type: builtin_earpiece, addr: CARD_0_DEV_1)',
    )

    device_identity_text_dump = """\
## Audio mode:
  - Requested mode = MODE_IN_COMMUNICATION
  - Actual mode = MODE_IN_COMMUNICATION

  Computed Preferred communication device: android.media.audio.DeviceIdentity{role: 2, type: 1, address: CARD_0_DEV_1}
  Applied Preferred communication device: android.media.audio.DeviceIdentity{role: 2, type: 1, address: CARD_0_DEV_1}
  Active communication device: android.media.audio.DeviceIdentity{role: 2, type: 1, address: CARD_0_DEV_1}

  AudioModeSession [8a9b0c]:
    package: com.android.cuttlefish.audiotest uid: 10142 pid: 4120
    mode: MODE_IN_COMMUNICATION (3)
    isDisplayActiveUseCase: false
    lifecycle: clientPaused=false paused=false closed=false hasFocus=true
    requestedRoute: Optional.empty
    targetRoute: Optional[android.media.audio.IAudioModeSession.Route{output: android.media.audio.DeviceIdentity{role: 2, type: 1, address: CARD_0_DEV_1}, input: null}]

  AudioModeSession [9d0e1f]:
    package: com.android.cuttlefish.audiotest uid: 10142 pid: 4120
    mode: MODE_IN_COMMUNICATION (3)
    isDisplayActiveUseCase: false
    lifecycle: clientPaused=true serverPaused=false closed=false hasFocus=false isLive=false
    requestedRoute: Optional.empty
"""
    parsed_di = audio_stack_inspector.parse_dumpsys_audio(
        device_identity_text_dump
    )
    di_sessions = parsed_di['mode_sessions']
    self.assertEqual(len(di_sessions), 2)
    self.assertFalse(di_sessions[0].paused)
    self.assertTrue(di_sessions[1].client_paused)
    self.assertTrue(di_sessions[1].paused)

    strategies, patches = audio_stack_inspector.parse_dumpsys_audio_policy(
        SAMPLE_DUMPSYS_AUDIO_POLICY
    )
    out_threads, in_threads = audio_stack_inspector.parse_dumpsys_audio_flinger(
        SAMPLE_DUMPSYS_AUDIO_FLINGER
    )
    di_snap = audio_stack_inspector.AudioStackSnapshot(
        requested_mode=str(parsed_di['requested_mode']),
        actual_mode=str(parsed_di['actual_mode']),
        computed_preferred_comm_device=str(
            parsed_di['computed_preferred_comm_device']
        ),
        applied_preferred_comm_device=str(
            parsed_di['applied_preferred_comm_device']
        ),
        active_comm_device=str(parsed_di['active_comm_device']),
        started_playback_usages=tuple(parsed_di['started_playback_usages']),
        mode_sessions=tuple(di_sessions),
        strategies=strategies,
        patches=patches,
        output_threads=out_threads,
        input_threads=in_threads,
        alsa_playback_running={0: False, 1: True},
        alsa_capture_running={0: True},
    )
    inspector = audio_stack_inspector.AudioStackInspector(mock.MagicMock())
    self.assertEqual(
        inspector.find_route_mismatches(
            di_snap,
            strategy_name='STRATEGY_PHONE',
            expected_device_token='AUDIO_DEVICE_OUT_EARPIECE',
            expected_address='CARD_0_DEV_1',
            expected_alsa_dev=1,
            inactive_alsa_devs=(0,),
            expected_mode='MODE_IN_COMMUNICATION',
        ),
        [],
    )
    wrong_type_mismatches = [
        m
        for m in inspector.find_route_mismatches(
            di_snap,
            strategy_name='STRATEGY_PHONE',
            expected_device_token='AUDIO_DEVICE_OUT_SPEAKER',
            expected_address='CARD_0_DEV_1',
            expected_alsa_dev=1,
            inactive_alsa_devs=(0,),
            expected_mode='MODE_IN_COMMUNICATION',
        )
        if m.startswith('Layer 1')
    ]
    self.assertEqual(len(wrong_type_mismatches), 2)

  def test_find_route_mismatches_rejects_wrong_bt_profile_sharing_mac_address(
      self,
  ):
    dut = mock.MagicMock()
    inspector = audio_stack_inspector.AudioStackInspector(dut)
    bt_addr = 'F0:F1:F2:F3:F4:F5'
    snap = audio_stack_inspector.AudioStackSnapshot(
        requested_mode='MODE_IN_COMMUNICATION',
        actual_mode='MODE_IN_COMMUNICATION',
        computed_preferred_comm_device=bt_addr,
        applied_preferred_comm_device=bt_addr,
        active_comm_device=bt_addr,
        started_playback_usages=('USAGE_VOICE_COMMUNICATION',),
        mode_sessions=(
            audio_stack_inspector.AudioModeSessionInfo(
                short_id='7f3a1b',
                mode='MODE_IN_COMMUNICATION',
                is_display_active_use_case=False,
                client_paused=False,
                paused=False,
                closed=False,
                has_focus=True,
                requested_route=bt_addr,
                target_route=bt_addr,
            ),
        ),
        strategies={
            'STRATEGY_PHONE': audio_stack_inspector.StrategyRoute(
                name='STRATEGY_PHONE',
                device_types='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
                address=bt_addr,
            ),
        },
        patches=(
            audio_stack_inspector.AudioPatchInfo(
                handle=42,
                src_mix_port_id=31,
                io_handle=29,
                sink_device_port_id=12,
                sink_device=f'AUDIO_DEVICE_OUT_BLUETOOTH_A2DP, @:{bt_addr}',
            ),
        ),
        output_threads={
            29: audio_stack_inspector.OutputThreadInfo(
                io_handle=29,
                standby=False,
                output_devices='0x10 (AUDIO_DEVICE_OUT_BLUETOOTH_SCO)',
                frames_written=48000,
                active_tracks=1,
            ),
        },
        input_threads={},
        alsa_playback_running={0: False, 1: False},
        alsa_capture_running={0: False},
    )
    mismatches = inspector.find_route_mismatches(
        snap,
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
        expected_address=bt_addr,
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_IN_COMMUNICATION',
    )
    self.assertEqual(len(mismatches), 1)
    self.assertIn('Layer 3 (AudioPatches)', mismatches[0])

    unapplied_broker_snap = dataclasses.replace(
        snap, applied_preferred_comm_device=''
    )
    broker_mismatches = inspector.find_route_mismatches(
        unapplied_broker_snap,
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
        expected_address=bt_addr,
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_IN_COMMUNICATION',
    )
    self.assertTrue(
        any('Layer 1 (AudioDeviceBroker)' in m for m in broker_mismatches)
    )

    empty_target_session = dataclasses.replace(
        snap.mode_sessions[0], target_route=''
    )
    empty_target_snap = dataclasses.replace(
        snap, mode_sessions=(empty_target_session,)
    )
    empty_target_mismatches = inspector.find_route_mismatches(
        empty_target_snap,
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
        expected_address='',
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_IN_COMMUNICATION',
    )
    self.assertTrue(
        any('Layer 1 (AudioService)' in m for m in empty_target_mismatches)
    )

    # Verify anonymized MAC address ("XX:XX:XX:XX:F4:F5") matches full expected_address ("F0:F1:F2:F3:F4:F5")
    self.assertEqual(
        audio_stack_inspector.anonymize_bluetooth_address(bt_addr),
        'XX:XX:XX:XX:F4:F5',
    )
    self.assertTrue(
        audio_stack_inspector.address_matches(
            'Route{output: DeviceIdentity{address: XX:XX:XX:XX:F4:F5}}',
            bt_addr,
        )
    )

    # Verify wait_for_route and wait_for_frames_advancing retry on transient ValueError
    valid_snap = dataclasses.replace(
        snap,
        patches=(
            audio_stack_inspector.AudioPatchInfo(
                handle=42,
                src_mix_port_id=31,
                io_handle=29,
                sink_device_port_id=12,
                sink_device=f'AUDIO_DEVICE_OUT_BLUETOOTH_SCO, @:{bt_addr}',
            ),
        ),
    )
    inspector.snapshot = mock.MagicMock(
        side_effect=[ValueError('transient decode error'), valid_snap]
    )
    converged, _ = inspector.wait_for_route(
        strategy_name='STRATEGY_PHONE',
        expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
        expected_address=bt_addr,
        expected_alsa_dev=None,
        inactive_alsa_devs=(0, 1),
        expected_mode='MODE_IN_COMMUNICATION',
        timeout_sec=1.0,
        poll_interval_sec=0.0,
    )
    self.assertTrue(converged)

    advancing_snap = dataclasses.replace(
        valid_snap,
        output_threads={
            29: audio_stack_inspector.OutputThreadInfo(
                io_handle=29,
                standby=False,
                output_devices='0x10 (AUDIO_DEVICE_OUT_BLUETOOTH_SCO)',
                frames_written=96000,
                active_tracks=1,
            ),
        },
    )
    inspector.snapshot = mock.MagicMock(
        side_effect=[
            ValueError('transient baseline error'),
            valid_snap,
            ValueError('transient poll error'),
            advancing_snap,
        ]
    )
    base_frames, latest_frames = inspector.wait_for_frames_advancing(
        'AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
        timeout_sec=1.0,
        poll_interval_sec=0.0,
    )
    self.assertEqual((base_frames, latest_frames), (48000, 96000))

  def test_find_route_mismatches_checks_proto_device_type_and_output_segment(
      self,
  ):
    dut = mock.MagicMock()
    inspector = audio_stack_inspector.AudioStackInspector(dut)
    bt_addr = 'F0:F1:F2:F3:F4:F5'
    anon_addr = 'XX:XX:XX:XX:F4:F5'

    def device(device_type: int, address: str, role: int = _ROLE_OUTPUT):
      return audio_stack_inspector._format_device_identity_proto(
          audioservice_pb2.DeviceIdentityProto(
              role=role, type=device_type, address=address
          )
      )

    def route(output: str, input_device: str = 'None'):
      return f'Route(output={output}, input={input_device})'

    sco_out = device(_TYPE_BLUETOOTH_SCO, anon_addr)
    a2dp_out = device(_TYPE_BLUETOOTH_A2DP, anon_addr)
    sco_in = device(_TYPE_BLUETOOTH_SCO, anon_addr, role=_ROLE_INPUT)
    earpiece = device(_TYPE_BUILTIN_EARPIECE, 'CARD_0_DEV_1')

    def snapshot(target_route: str, applied_device: str):
      return audio_stack_inspector.AudioStackSnapshot(
          requested_mode='MODE_IN_COMMUNICATION',
          actual_mode='MODE_IN_COMMUNICATION',
          computed_preferred_comm_device=applied_device,
          applied_preferred_comm_device=applied_device,
          active_comm_device=applied_device,
          started_playback_usages=('USAGE_VOICE_COMMUNICATION',),
          mode_sessions=(
              audio_stack_inspector.AudioModeSessionInfo(
                  short_id='7f3a1b',
                  mode='MODE_IN_COMMUNICATION',
                  is_display_active_use_case=False,
                  client_paused=False,
                  paused=False,
                  closed=False,
                  has_focus=True,
                  requested_route=target_route,
                  target_route=target_route,
              ),
          ),
          strategies={
              'STRATEGY_PHONE': audio_stack_inspector.StrategyRoute(
                  name='STRATEGY_PHONE',
                  device_types='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
                  address=bt_addr,
              ),
          },
          patches=(
              audio_stack_inspector.AudioPatchInfo(
                  handle=42,
                  src_mix_port_id=31,
                  io_handle=29,
                  sink_device_port_id=12,
                  sink_device=f'AUDIO_DEVICE_OUT_BLUETOOTH_SCO, @:{bt_addr}',
              ),
          ),
          output_threads={
              29: audio_stack_inspector.OutputThreadInfo(
                  io_handle=29,
                  standby=False,
                  output_devices='0x10 (AUDIO_DEVICE_OUT_BLUETOOTH_SCO)',
                  frames_written=48000,
                  active_tracks=1,
              ),
          },
          input_threads={},
          alsa_playback_running={0: False, 1: False},
          alsa_capture_running={0: False},
      )

    def layer1_mismatches(snap, expected_address=bt_addr):
      return [
          m
          for m in inspector.find_route_mismatches(
              snap,
              strategy_name='STRATEGY_PHONE',
              expected_device_token='AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
              expected_address=expected_address,
              expected_alsa_dev=None,
              inactive_alsa_devs=(0, 1),
              expected_mode='MODE_IN_COMMUNICATION',
          )
          if m.startswith('Layer 1')
      ]

    # SCO output with the matching anonymized MAC converges.
    self.assertEqual(layer1_mismatches(snapshot(route(sco_out), sco_out)), [])

    # A2DP (type=8) sharing the MAC address of the SCO (type=7) profile is a
    # different device, both in the session route and in the broker.
    a2dp_mismatches = layer1_mismatches(snapshot(route(a2dp_out), a2dp_out))
    self.assertEqual(len(a2dp_mismatches), 2)
    self.assertIn('Layer 1 (AudioService)', a2dp_mismatches[0])
    self.assertIn('Layer 1 (AudioDeviceBroker)', a2dp_mismatches[1])

    # The expected address on the input= device does not make the route match
    # when output= is routed elsewhere.
    self.assertEqual(
        len(layer1_mismatches(snapshot(route(earpiece, sco_in), sco_out))), 1
    )

    # An empty expected address still requires the expected device type.
    self.assertEqual(
        layer1_mismatches(snapshot(route(sco_out), sco_out), ''), []
    )
    self.assertEqual(
        len(layer1_mismatches(snapshot(route(earpiece), earpiece), '')), 2
    )

    # Text-dump routes without a numeric type still match by address only.
    text_route = f'Device(type: bluetooth_sco_hs, addr: {anon_addr})'
    self.assertEqual(
        layer1_mismatches(snapshot(text_route, text_route)), []
    )

    # An unset output device, an invalid (negative) type, or a DeviceIdentity
    # without a type never matches, even with an empty expected address.
    for bad in (
        route('None'),
        'None',
        'null',
        '',
        route(device(-1, anon_addr)),
        f'DeviceIdentity(role=2, address={anon_addr})',
    ):
      for expected_address in (bt_addr, ''):
        self.assertFalse(
            audio_stack_inspector.device_matches(
                audio_stack_inspector.route_output(bad),
                'AUDIO_DEVICE_OUT_BLUETOOTH_SCO',
                expected_address,
            ),
            msg=f'{bad!r} with expected_address={expected_address!r}',
        )

    # Every token mirrors AudioDeviceInfo.INT_TO_EXT_DEVICE_MAPPING, so the
    # newer tokens disambiguate too.
    for token, ext_type in (
        ('AUDIO_DEVICE_OUT_BLE_HEARING_AID', 33),
        ('AUDIO_DEVICE_OUT_BLE_CENTRAL', 34),
        ('AUDIO_DEVICE_OUT_BUS', 21),
        ('AUDIO_DEVICE_OUT_SPEAKER_SAFE', 24),
    ):
      self.assertTrue(
          audio_stack_inspector.device_matches(
              device(ext_type, anon_addr), token, bt_addr
          )
      )
      self.assertFalse(
          audio_stack_inspector.device_matches(
              device(ext_type + 1, anon_addr), token, bt_addr
          )
      )
    # BLE hearing aids (type 33) are not BLE headsets (type 26).
    self.assertFalse(
        audio_stack_inspector.device_matches(
            device(33, anon_addr), 'AUDIO_DEVICE_OUT_BLE_HEADSET', bt_addr
        )
    )
    # Unmapped tokens (e.g. input tokens) fail closed on DeviceIdentity strings.
    for unmapped_token in (
        'AUDIO_DEVICE_IN_BUILTIN_MIC',
        'AUDIO_DEVICE_OUT_UNKNOWN',
    ):
      for expected_address in (bt_addr, ''):
        self.assertFalse(
            audio_stack_inspector.device_matches(
                sco_out, unmapped_token, expected_address
            )
        )

    self.assertEqual(
        audio_stack_inspector.route_output(
            f'Route{{output: DeviceIdentity{{role: 2, type: 7, address: {anon_addr}}},'
            ' input: null}'
        ),
        f'DeviceIdentity{{role: 2, type: 7, address: {anon_addr}}}',
    )


if __name__ == '__main__':
  unittest.main(verbosity=2)
