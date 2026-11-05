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
"""5-Layer Audio Stack Inspector for Cuttlefish E2E Audio Tests.

Inspects and correlates routing and streaming state across:
  Layer 1: AudioService (`dumpsys audio`)
  Layer 2: AudioPolicyManager Product Strategies (`dumpsys media.audio_policy`)
  Layer 3: AudioPolicyManager Audio Patches (`dumpsys media.audio_policy`)
  Layer 4: AudioFlinger Mixer & Record Threads (`dumpsys media.audio_flinger`)
  Layer 5: ALSA / VirtIO-Snd Kernel PCM State (`/proc/asound/card0/pcm*/sub0/status`)
"""

import dataclasses
import re
import time
from typing import Mapping, Sequence

from frameworks.base.core.proto.android.media import audioattributes_pb2
from frameworks.base.core.proto.android.server import audioservice_pb2
from google.protobuf import message

DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_POLL_INTERVAL_SEC = 0.25
_DUMP_EXCERPT_MAX_CHARS = 240


def _require_section_anchor(dump: str, anchor: str, service_name: str) -> None:
  """Raises ValueError with a dump excerpt if `anchor` is absent from `dump`."""
  if anchor not in dump:
    excerpt = dump[:_DUMP_EXCERPT_MAX_CHARS].replace('\n', '\\n')
    raise ValueError(
        f'Missing section anchor {anchor!r} in {service_name} dump;'
        f' excerpt={excerpt!r}'
    )


@dataclasses.dataclass(frozen=True)
class StrategyRoute:
  """Selected routing state for a ProductStrategy in `dumpsys media.audio_policy`."""

  name: str
  device_types: str
  address: str


@dataclasses.dataclass(frozen=True)
class AudioPatchInfo:
  """Active audio patch entry in `dumpsys media.audio_policy`."""

  handle: int
  src_mix_port_id: int
  io_handle: int
  sink_device_port_id: int
  sink_device: str


@dataclasses.dataclass(frozen=True)
class OutputThreadInfo:
  """AudioFlinger PlaybackThread snapshot from `dumpsys media.audio_flinger`."""

  io_handle: int
  standby: bool
  output_devices: str
  frames_written: int
  active_tracks: int


@dataclasses.dataclass(frozen=True)
class InputThreadInfo:
  """AudioFlinger RecordThread snapshot from `dumpsys media.audio_flinger`."""

  io_handle: int
  standby: bool
  input_device: str
  audio_source: str


@dataclasses.dataclass(frozen=True)
class AudioModeSessionInfo:
  """AudioModeSession snapshot from `dumpsys audio`."""

  short_id: str
  mode: str
  is_display_active_use_case: bool
  client_paused: bool
  paused: bool
  closed: bool
  has_focus: bool
  requested_route: str
  target_route: str


@dataclasses.dataclass(frozen=True)
class FocusConfig:
  """MediaFocusControl configuration flags from `dumpsys audio`."""

  has_focus_policy: bool
  multi_audio_focus_enabled: bool


@dataclasses.dataclass(frozen=True)
class AudioStackSnapshot:
  """Correlated 5-layer snapshot of the Android guest audio stack."""

  # Layer 1: AudioService
  requested_mode: str
  actual_mode: str
  computed_preferred_comm_device: str
  applied_preferred_comm_device: str
  active_comm_device: str
  started_playback_usages: tuple[str, ...]
  mode_sessions: tuple[AudioModeSessionInfo, ...]

  # Layer 2 & 3: AudioPolicyManager
  strategies: Mapping[str, StrategyRoute]
  patches: tuple[AudioPatchInfo, ...]

  # Layer 4: AudioFlinger
  output_threads: Mapping[int, OutputThreadInfo]
  input_threads: Mapping[int, InputThreadInfo]

  # Layer 5: ALSA / VirtIO-Snd (/proc/asound/card0)
  alsa_playback_running: Mapping[int, bool]
  alsa_capture_running: Mapping[int, bool]


_REQ_MODE_RE = re.compile(r'^\s*-\s*Requested mode\s*=\s*(\S+)', re.MULTILINE)
_ACT_MODE_RE = re.compile(r'^\s*-\s*Actual mode\s*=\s*(\S+)', re.MULTILINE)
_COMP_COMM_RE = re.compile(
    r'^\s*Computed Preferred communication device:\s*(.+)$', re.MULTILINE
)
_APPL_COMM_RE = re.compile(
    r'^\s*Applied Preferred communication device:\s*(.+)$', re.MULTILINE
)
_ACT_COMM_RE = re.compile(
    r'^\s*Active communication device:\s*(.+)$', re.MULTILINE
)
_PLAYBACK_STARTED_RE = re.compile(
    r'AudioPlaybackConfiguration\s+piid:\d+.*?\bstate:started\b.*?\busage=(\S+)',
    re.MULTILINE,
)
_MODE_SESSION_RE = re.compile(
    r'AudioModeSession\s+\[([^\]]+)\]:\s*\n'
    r'(?:[ \t]+(?!AudioModeSession\b).*\n)*?'
    r'\s+mode:\s*(\S+)\s*\(-?\d+\)\s*\n'
    r'\s+isDisplayActiveUseCase:\s*(true|false)\s*\n'
    r'\s+lifecycle:\s*clientPaused=(true|false)\s+(?:serverPaused|paused)=(true|false)'
    r'\s+closed=(true|false)\s+hasFocus=(true|false)(?:\s+isLive=(?:true|false))?\s*\n'
    r'\s+requestedRoute:\s*(.+)(?:\n[ \t]+(?:appliedRoute|targetRoute):\s*(.+))?',
    re.MULTILINE,
)

_STRATEGY_RE = re.compile(
    r'^\s*-(STRATEGY_\S+)\s+\(id:\s*\d+\)\s*(?:\(ZoneId:\s*\d+\))?\s*\n'
    r'\s*Selected Device:\s*\{([^}]*),\s*@:([^}]*)\}',
    re.MULTILINE,
)

_PATCH_RE = re.compile(
    r'owner uid\s+\d+;\s*handle\s+(\d+);\s*HAL handle\s+\d+\s*\n'
    r'(?:\s*\[src\s+\d+\]\s*Device Port ID:[^\n]*\n)*'
    r'\s*\[src\s+\d+\]\s*Mix Port ID:\s*(\d+);\s*I/O handle:\s*(\d+);\s*\n'
    r'\s*\[sink\s+1\]\s*Device Port ID:\s*(\d+);\s*\{([^}]*)\}',
    re.MULTILINE,
)

_THREAD_HEADER_RE = re.compile(
    r'^(Output|Input) thread 0x[0-9a-fA-F]+,\s*name\s+\S+,\s*tid\s+\d+,'
    r'\s*type\s+\d+\s*\(([^)]+)\):',
    re.MULTILINE,
)
_FLINGER_THREAD_END_RE = re.compile(
    r'\n(?:Historical Thread Log|\s*## BEGIN (?!thread dump))'
)
_IO_HANDLE_RE = re.compile(r'^\s*I/O handle:\s*(\d+)', re.MULTILINE)
_STANDBY_RE = re.compile(r'^\s*Standby:\s*(\S+)', re.MULTILINE)
_OUT_DEVICES_RE = re.compile(r'^\s*Output devices:\s*(.+)$', re.MULTILINE)
_IN_DEVICE_RE = re.compile(r'^\s*Input device:\s*(.+)$', re.MULTILINE)
_AUDIO_SOURCE_RE = re.compile(r'^\s*Audio source:\s*(.+)$', re.MULTILINE)
_FRAMES_WRITTEN_RE = re.compile(r'^\s*Frames written:\s*(\d+)', re.MULTILINE)
_ACTIVE_TRACKS_RE = re.compile(
    r'^\s*\d+\s+Tracks(?:\s+of which\s+(\d+)\s+are active)?', re.MULTILINE
)
_FOCUS_STACK_HEADER = 'Audio Focus stack entries (last is top of stack):'
_FOCUS_STACK_TERMINATORS = (
    'No external focus policy',
    'External focus policy:',
    ' Notify on duck:',
)
_BT_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$')


def anonymize_bluetooth_address(address: str) -> str:
  """Anonymizes a 6-octet Bluetooth MAC address to `XX:XX:XX:XX:EE:FF`."""
  if _BT_MAC_RE.match(address):
    return 'XX:XX:XX:XX' + address[11:]
  return address


def address_matches(actual: str, expected: str) -> bool:
  """Returns True if `actual` matches `expected` or its anonymized MAC form."""
  if not expected:
    return True
  anon_expected = anonymize_bluetooth_address(expected)
  return expected in actual or anon_expected in actual


# External `AudioDeviceInfo.TYPE_*` value that `dumpsys audio --proto` writes
# in `DeviceIdentityProto.type`, keyed by the internal `AUDIO_DEVICE_OUT_*`
# token that Layers 2-4 report. Mirrors the output half of
# `AudioDeviceInfo.INT_TO_EXT_DEVICE_MAPPING` (frameworks/base). Several
# internal tokens share one external type (e.g. the three SCO tokens).
_DEVICE_TOKEN_EXTERNAL_TYPE: Mapping[str, int] = {
    'AUDIO_DEVICE_OUT_EARPIECE': 1,  # TYPE_BUILTIN_EARPIECE
    'AUDIO_DEVICE_OUT_SPEAKER': 2,  # TYPE_BUILTIN_SPEAKER
    'AUDIO_DEVICE_OUT_WIRED_HEADSET': 3,  # TYPE_WIRED_HEADSET
    'AUDIO_DEVICE_OUT_WIRED_HEADPHONE': 4,  # TYPE_WIRED_HEADPHONES
    'AUDIO_DEVICE_OUT_LINE': 5,  # TYPE_LINE_ANALOG
    'AUDIO_DEVICE_OUT_SPDIF': 6,  # TYPE_LINE_DIGITAL
    'AUDIO_DEVICE_OUT_BLUETOOTH_SCO': 7,  # TYPE_BLUETOOTH_SCO
    'AUDIO_DEVICE_OUT_BLUETOOTH_SCO_HEADSET': 7,
    'AUDIO_DEVICE_OUT_BLUETOOTH_SCO_CARKIT': 7,
    'AUDIO_DEVICE_OUT_BLUETOOTH_A2DP': 8,  # TYPE_BLUETOOTH_A2DP
    'AUDIO_DEVICE_OUT_BLUETOOTH_A2DP_HEADPHONES': 8,
    'AUDIO_DEVICE_OUT_BLUETOOTH_A2DP_SPEAKER': 8,
    'AUDIO_DEVICE_OUT_HDMI': 9,  # TYPE_HDMI
    'AUDIO_DEVICE_OUT_HDMI_ARC': 10,  # TYPE_HDMI_ARC
    'AUDIO_DEVICE_OUT_USB_DEVICE': 11,  # TYPE_USB_DEVICE
    'AUDIO_DEVICE_OUT_USB_ACCESSORY': 12,  # TYPE_USB_ACCESSORY
    'AUDIO_DEVICE_OUT_DGTL_DOCK_HEADSET': 13,  # TYPE_DOCK
    'AUDIO_DEVICE_OUT_FM': 14,  # TYPE_FM
    'AUDIO_DEVICE_OUT_TELEPHONY_TX': 18,  # TYPE_TELEPHONY
    'AUDIO_DEVICE_OUT_AUX_LINE': 19,  # TYPE_AUX_LINE
    'AUDIO_DEVICE_OUT_IP': 20,  # TYPE_IP
    'AUDIO_DEVICE_OUT_BUS': 21,  # TYPE_BUS
    'AUDIO_DEVICE_OUT_USB_HEADSET': 22,  # TYPE_USB_HEADSET
    'AUDIO_DEVICE_OUT_HEARING_AID': 23,  # TYPE_HEARING_AID
    'AUDIO_DEVICE_OUT_SPEAKER_SAFE': 24,  # TYPE_BUILTIN_SPEAKER_SAFE
    'AUDIO_DEVICE_OUT_REMOTE_SUBMIX': 25,  # TYPE_REMOTE_SUBMIX
    'AUDIO_DEVICE_OUT_BLE_HEADSET': 26,  # TYPE_BLE_HEADSET
    'AUDIO_DEVICE_OUT_BLE_SPEAKER': 27,  # TYPE_BLE_SPEAKER
    'AUDIO_DEVICE_OUT_HDMI_EARC': 29,  # TYPE_HDMI_EARC
    'AUDIO_DEVICE_OUT_BLE_BROADCAST': 30,  # TYPE_BLE_BROADCAST
    'AUDIO_DEVICE_OUT_ANLG_DOCK_HEADSET': 31,  # TYPE_DOCK_ANALOG
    'AUDIO_DEVICE_OUT_MULTICHANNEL_GROUP': 32,  # TYPE_MULTICHANNEL_GROUP
    'AUDIO_DEVICE_OUT_BLE_HEARING_AID': 33,  # TYPE_BLE_HEARING_AID
    'AUDIO_DEVICE_OUT_BLE_CENTRAL': 34,  # TYPE_BLE_CENTRAL
}

# `output=`/`output:` segment of a formatted route, in either the proto form
# `Route(output=DeviceIdentity(...), input=...)` or the AIDL text form
# `Route{output: DeviceIdentity{...}, input: ...}`.
_ROUTE_OUTPUT_RE = re.compile(r'\boutput[=:]\s*(.*?)(?:,\s*input[=:]|\}?\)?$)')
# `type=<n>`/`type: <n>` of a formatted `DeviceIdentity` (may be negative).
_DEVICE_TYPE_RE = re.compile(r'\btype[=:]\s*(-?\d+)\b')
# Placeholders for an unset device in formatted routes and broker fields.
_UNSET_DEVICE_VALUES = frozenset({'', 'None', 'null', 'Optional.empty'})


def route_output(route: str) -> str:
  """Returns the output device segment of a formatted route, or `route`.

  Formatted routes with an `output=`/`output:` segment (see
  `_format_route_field` and `IAudioModeSession.Route.toString()`) are
  narrowed to that segment so that an `input=` device sharing the expected
  address does not make the route match. Other strings are returned as is.
  """
  match = _ROUTE_OUTPUT_RE.search(route)
  return match.group(1).strip() if match else route


def device_matches(
    actual: str, expected_device_token: str, expected_address: str
) -> bool:
  """Returns True if a formatted device matches the expected type and address.

  An unset device (`''`, `None`, `null`, `Optional.empty`) never matches. When
  `actual` is a formatted `DeviceIdentity`, `expected_device_token` must map to
  an external `AudioDeviceInfo.TYPE_*` value and `actual`'s `type=<n>` must be
  present and equal to that value; this separates the HFP SCO, A2DP, and BLE
  profiles of one headset that share a MAC address, and rejects unmapped tokens
  and wrong, missing, or invalid (negative) types even when `expected_address`
  is empty. Strings without a `DeviceIdentity` (legacy text-dump devices) are
  matched by address only.
  """
  actual = actual.strip()
  if actual in _UNSET_DEVICE_VALUES:
    return False
  if 'DeviceIdentity' in actual:
    expected_type = _DEVICE_TOKEN_EXTERNAL_TYPE.get(expected_device_token)
    if expected_type is None:
      return False
    type_match = _DEVICE_TYPE_RE.search(actual)
    if type_match is None or int(type_match.group(1)) != expected_type:
      return False
  return address_matches(actual, expected_address)


def parse_focus_stack(dump: str) -> str:
  """Extracts the active MediaFocusControl stack slice from `dumpsys audio`.

  Slices strictly between `Audio Focus stack entries (last is top of stack):`
  and the focus-stack terminator (`No external focus policy`,
  `External focus policy:`, or ` Notify on duck:`) so historical `EventLogger`
  entries in `dumpsys audio` are never matched.

  Args:
    dump: Full text output of `adb shell dumpsys audio`.

  Returns:
    Newline-joined active focus stack lines.

  Raises:
    ValueError: If the focus stack header or terminator is missing from `dump`.
  """
  _require_section_anchor(
      dump, _FOCUS_STACK_HEADER, 'audio (MediaFocusControl stack)'
  )
  _, _, tail = dump.partition(_FOCUS_STACK_HEADER)
  stack_lines: list[str] = []
  found_terminator = False
  for line in tail.splitlines():
    if line.startswith(_FOCUS_STACK_TERMINATORS):
      found_terminator = True
      break
    stack_lines.append(line)
  if not found_terminator:
    raise ValueError(
        'Missing expected focus stack terminator'
        f' {_FOCUS_STACK_TERMINATORS!r} in dumpsys audio'
    )
  return '\n'.join(stack_lines)


def _format_usage_proto(usage_val: int) -> str:
  try:
    name = audioattributes_pb2.Usage.Name(usage_val)
  except ValueError:
    return f'USAGE_INVALID_{usage_val}'
  return name if name.startswith('USAGE_') else f'USAGE_{name}'


def _format_content_type_proto(content_val: int) -> str:
  try:
    name = audioattributes_pb2.ContentType.Name(content_val)
  except ValueError:
    return f'CONTENT_TYPE_INVALID_{content_val}'
  return (
      name if name.startswith('CONTENT_TYPE_') else f'CONTENT_TYPE_{name}'
  )


def _unwrap_optional_route(val: str) -> str:
  if val.startswith('Optional[') and val.endswith(']'):
    return val[9:-1]
  return val


def _format_device_identity_proto(
    device: audioservice_pb2.DeviceIdentityProto,
) -> str:
  """Formats a `DeviceIdentityProto` for address matching and failure logs."""
  return (
      f'DeviceIdentity(role={device.role}, type={device.type},'
      f' address={device.address})'
  )


def _format_device_field(msg: message.Message, field: str) -> str:
  """Formats the `DeviceIdentityProto` in `field` of `msg`, or '' if unset."""
  if not msg.HasField(field):
    return ''
  return _format_device_identity_proto(getattr(msg, field))


def _format_route_field(msg: message.Message, field: str) -> str:
  """Formats the `AudioModeSessionProto.Route` in `field` of `msg`, or ''."""
  if not msg.HasField(field):
    return ''
  route = getattr(msg, field)
  output = _format_device_field(route, 'output') or 'None'
  input_device = _format_device_field(route, 'input') or 'None'
  return f'Route(output={output}, input={input_device})'


def parse_focus_stack_proto(raw_bytes: bytes) -> str:
  """Extracts active MediaFocusControl stack entries from `dumpsys audio --proto`."""
  if not raw_bytes:
    raise ValueError('Empty dumpsys audio --proto output')
  try:
    dump_proto = audioservice_pb2.AudioServiceDumpProto.FromString(raw_bytes)
  except message.DecodeError as exc:
    raise ValueError(
        f'Invalid dumpsys audio --proto payload ({len(raw_bytes)} bytes,'
        f' prefix={raw_bytes[:80]!r}): {exc}'
    ) from exc
  if not dump_proto.HasField('audio_mode'):
    raise ValueError(
        'Missing audio_mode field in dumpsys audio --proto output'
        f' ({len(raw_bytes)} bytes, prefix={raw_bytes[:80]!r})'
    )
  mfc = dump_proto.media_focus_control
  lines: list[str] = []
  for req in mfc.focus_stack:
    usage_name = _format_usage_proto(req.audio_attributes.usage)
    content_name = _format_content_type_proto(
        req.audio_attributes.content_type
    )
    lines.append(
        f'pack: {req.package_name} -- client: {req.client_id} -- gain:'
        f' {req.focus_gain} -- flags: {req.grant_flags} -- loss:'
        f' {req.focus_loss_received} -- notified:'
        f' {str(req.focus_loss_was_notified).lower()} -- uid:'
        f' {req.calling_uid} -- attr: AudioAttributes: usage={usage_name}'
        f' content={content_name} flags=0x{req.audio_attributes.flags:x} --'
        f' sdk:{req.sdk_target}'
    )
  return '\n'.join(lines)


def parse_focus_config(dump: str) -> FocusConfig:
  """Extracts MediaFocusControl configuration flags from text `dumpsys audio`."""
  _require_section_anchor(
      dump, _FOCUS_STACK_HEADER, 'audio (MediaFocusControl config)'
  )
  return FocusConfig(
      has_focus_policy='has focus policy:true' in dump
      or 'External focus policy:' in dump,
      multi_audio_focus_enabled='Multi Audio Focus enabled :true' in dump,
  )


def parse_focus_config_proto(raw_bytes: bytes) -> FocusConfig:
  """Extracts MediaFocusControl configuration flags from `dumpsys audio --proto`."""
  if not raw_bytes:
    raise ValueError('Empty dumpsys audio --proto output')
  try:
    dump_proto = audioservice_pb2.AudioServiceDumpProto.FromString(raw_bytes)
  except message.DecodeError as exc:
    raise ValueError(
        f'Invalid dumpsys audio --proto payload ({len(raw_bytes)} bytes,'
        f' prefix={raw_bytes[:80]!r}): {exc}'
    ) from exc
  if not dump_proto.HasField('audio_mode'):
    raise ValueError(
        'Missing audio_mode field in dumpsys audio --proto output'
        f' ({len(raw_bytes)} bytes, prefix={raw_bytes[:80]!r})'
    )
  mfc = dump_proto.media_focus_control
  return FocusConfig(
      has_focus_policy=mfc.has_focus_policy,
      multi_audio_focus_enabled=mfc.multi_audio_focus_enabled,
  )


def parse_dumpsys_audio_proto(raw_bytes: bytes) -> dict[str, object]:
  """Parses Layer 1 fields from `dumpsys audio --proto` (`AudioServiceDumpProto`)."""
  if not raw_bytes:
    raise ValueError('Empty dumpsys audio --proto output')
  try:
    dump_proto = audioservice_pb2.AudioServiceDumpProto.FromString(raw_bytes)
  except message.DecodeError as exc:
    raise ValueError(
        f'Invalid dumpsys audio --proto payload ({len(raw_bytes)} bytes,'
        f' prefix={raw_bytes[:80]!r}): {exc}'
    ) from exc
  if not dump_proto.HasField('audio_mode'):
    raise ValueError(
        'Missing audio_mode field in dumpsys audio --proto output'
        f' ({len(raw_bytes)} bytes, prefix={raw_bytes[:80]!r})'
    )
  started_usages: list[str] = []
  for player in dump_proto.playback_monitor.players:
    if (
        player.player_state
        == audioservice_pb2.AudioPlaybackConfigurationProto.PLAYER_STATE_STARTED
    ):
      started_usages.append(
          _format_usage_proto(player.audio_attributes.usage)
      )
  mode_sessions = tuple(
      AudioModeSessionInfo(
          short_id=s.short_id,
          mode=s.mode,
          is_display_active_use_case=s.is_display_active_use_case,
          client_paused=s.client_paused,
          paused=s.paused,
          closed=s.closed,
          has_focus=s.has_focus,
          requested_route=_format_route_field(s, 'requested_route'),
          target_route=_format_route_field(s, 'target_route'),
      )
      for s in dump_proto.device_broker.mode_sessions
  )
  return {
      'requested_mode': dump_proto.audio_mode.requested_mode,
      'actual_mode': dump_proto.audio_mode.actual_mode,
      'computed_preferred_comm_device': _format_device_field(
          dump_proto.device_broker, 'computed_preferred_communication_device'
      ),
      'applied_preferred_comm_device': _format_device_field(
          dump_proto.device_broker, 'applied_preferred_communication_device'
      ),
      'active_comm_device': _format_device_field(
          dump_proto.device_broker, 'active_communication_device'
      ),
      'started_playback_usages': tuple(started_usages),
      'mode_sessions': mode_sessions,
  }


def parse_dumpsys_audio(dump: str) -> dict[str, object]:
  """Parses Layer 1 fields from `dumpsys audio`."""
  _require_section_anchor(dump, '## Audio mode', 'audio')
  req_match = _REQ_MODE_RE.search(dump)
  act_match = _ACT_MODE_RE.search(dump)
  comp_match = _COMP_COMM_RE.search(dump)
  appl_match = _APPL_COMM_RE.search(dump)
  act_comm_match = _ACT_COMM_RE.search(dump)
  comp_comm = comp_match.group(1).strip() if comp_match else ''
  started_usages = tuple(_PLAYBACK_STARTED_RE.findall(dump))
  mode_sessions: list[AudioModeSessionInfo] = []
  for match in _MODE_SESSION_RE.finditer(dump):
    client_paused = match.group(4) == 'true'
    closed = match.group(6) == 'true'
    paused = (match.group(5) == 'true') or client_paused or closed
    requested_route = _unwrap_optional_route(match.group(8).strip())
    raw_target = (
        _unwrap_optional_route(match.group(9).strip())
        if match.group(9) is not None
        else ''
    )
    if raw_target and raw_target not in ('Optional.empty', 'null', 'None'):
      target_route = raw_target
    elif requested_route not in ('', 'Optional.empty', 'null', 'None'):
      target_route = requested_route
    else:
      target_route = comp_comm
    mode_sessions.append(
        AudioModeSessionInfo(
            short_id=match.group(1).strip(),
            mode=match.group(2).strip(),
            is_display_active_use_case=match.group(3) == 'true',
            client_paused=client_paused,
            paused=paused,
            closed=closed,
            has_focus=match.group(7) == 'true',
            requested_route=requested_route,
            target_route=target_route,
        )
    )
  return {
      'requested_mode': req_match.group(1).strip() if req_match else '',
      'actual_mode': act_match.group(1).strip() if act_match else '',
      'computed_preferred_comm_device': comp_comm,
      'applied_preferred_comm_device': (
          appl_match.group(1).strip() if appl_match else ''
      ),
      'active_comm_device': (
          act_comm_match.group(1).strip() if act_comm_match else ''
      ),
      'started_playback_usages': started_usages,
      'mode_sessions': tuple(mode_sessions),
  }


def parse_dumpsys_audio_policy(
    dump: str,
) -> tuple[dict[str, StrategyRoute], tuple[AudioPatchInfo, ...]]:
  """Parses Layer 2 (ProductStrategies) and Layer 3 (AudioPatches) from APM."""
  _require_section_anchor(dump, 'owner uid', 'media.audio_policy')
  strategies: dict[str, StrategyRoute] = {}
  for match in _STRATEGY_RE.finditer(dump):
    name = match.group(1).strip()
    dev_types = match.group(2).strip()
    addr = match.group(3).strip()
    strategies[name] = StrategyRoute(
        name=name,
        device_types=dev_types,
        address=addr,
    )

  patches: list[AudioPatchInfo] = []
  for match in _PATCH_RE.finditer(dump):
    patches.append(
        AudioPatchInfo(
            handle=int(match.group(1)),
            src_mix_port_id=int(match.group(2)),
            io_handle=int(match.group(3)),
            sink_device_port_id=int(match.group(4)),
            sink_device=match.group(5).strip(),
        )
    )
  return strategies, tuple(patches)


def parse_dumpsys_audio_flinger(
    dump: str,
) -> tuple[dict[int, OutputThreadInfo], dict[int, InputThreadInfo]]:
  """Parses Layer 4 (Output and Input threads) from `dumpsys media.audio_flinger`."""
  _require_section_anchor(dump, 'Output thread', 'media.audio_flinger')
  output_threads: dict[int, OutputThreadInfo] = {}
  input_threads: dict[int, InputThreadInfo] = {}

  end_match = _FLINGER_THREAD_END_RE.search(dump)
  active_dump = dump[: end_match.start()] if end_match else dump

  matches = list(_THREAD_HEADER_RE.finditer(active_dump))
  for idx, match in enumerate(matches):
    direction = match.group(1)
    start = match.end()
    end = (
        matches[idx + 1].start() if idx + 1 < len(matches) else len(active_dump)
    )
    block = active_dump[start:end]

    io_match = _IO_HANDLE_RE.search(block)
    if not io_match:
      continue
    io_handle = int(io_match.group(1))
    standby_match = _STANDBY_RE.search(block)
    standby = (
        standby_match.group(1).strip().lower() == 'yes'
        if standby_match
        else True
    )

    if direction == 'Output':
      out_dev_match = _OUT_DEVICES_RE.search(block)
      frames_match = _FRAMES_WRITTEN_RE.search(block)
      tracks_match = _ACTIVE_TRACKS_RE.search(block)
      active_tracks = (
          int(tracks_match.group(1))
          if (tracks_match and tracks_match.group(1))
          else 0
      )
      output_threads[io_handle] = OutputThreadInfo(
          io_handle=io_handle,
          standby=standby,
          output_devices=(
              out_dev_match.group(1).strip() if out_dev_match else ''
          ),
          frames_written=int(frames_match.group(1)) if frames_match else 0,
          active_tracks=active_tracks,
      )
    else:
      in_dev_match = _IN_DEVICE_RE.search(block)
      source_match = _AUDIO_SOURCE_RE.search(block)
      input_threads[io_handle] = InputThreadInfo(
          io_handle=io_handle,
          standby=standby,
          input_device=in_dev_match.group(1).strip() if in_dev_match else '',
          audio_source=source_match.group(1).strip() if source_match else '',
      )

  return output_threads, input_threads


class AudioStackInspector:
  """Captures and verifies 5-layer audio stack snapshots on an AndroidDevice."""

  def __init__(self, dut):
    self._dut = dut

  def _shell(self, cmd: str) -> str:
    return self._dut.adb.shell(cmd).decode('utf-8', errors='replace')

  def _shell_bytes(self, cmd: str) -> bytes:
    return self._dut.adb.shell(cmd)

  def get_focus_stack(self) -> str:
    """Reads `dumpsys audio --proto` (or text fallback) for active focus stack."""
    raw = self._shell_bytes('dumpsys audio --proto')
    if raw.lstrip().startswith(b'AudioService Dumpsys'):
      return parse_focus_stack(raw.decode('utf-8', errors='replace'))
    return parse_focus_stack_proto(raw)

  def get_focus_config(self) -> FocusConfig:
    """Reads `dumpsys audio --proto` (or text fallback) for focus config flags."""
    raw = self._shell_bytes('dumpsys audio --proto')
    if raw.lstrip().startswith(b'AudioService Dumpsys'):
      return parse_focus_config(raw.decode('utf-8', errors='replace'))
    return parse_focus_config_proto(raw)

  def has_alsa_playback_device(self, device_id: int, card_id: int = 0) -> bool:
    """Returns True if `/dev/snd/pcmC<card_id>D<device_id>p` exists."""
    dev_path = f'/dev/snd/pcmC{card_id}D{device_id}p'
    proc_path = f'/proc/asound/card{card_id}/pcm{device_id}p/sub0/status'
    out = self._shell(
        f'(test -e {dev_path} || test -e {proc_path}) && echo 1 || echo 0'
    ).strip()
    return out == '1'

  def read_alsa_running_states(
      self,
      playback_devices: Sequence[int] = (0, 1),
      capture_devices: Sequence[int] = (0,),
      card_id: int = 0,
  ) -> tuple[dict[int, bool], dict[int, bool]]:
    """Reads ALSA active state for playback and capture substreams in one shell call."""
    # Check both `/proc/asound/.../status` (`state: RUNNING`) and open `/dev/snd/pcm*`
    # file descriptors in the AIDL Audio HAL (`StreamAlsa` opens the TinyALSA PCM
    # proxy only while active and closes it in `standby()`).
    script_parts = [
        'echo "===HAL_FDS==="',
        (
            'for p in $(pidof android.hardware.audio.service-aidl.example);'
            ' do ls -l "/proc/$p/fd" 2>/dev/null; done'
        ),
    ]
    for dev_id in playback_devices:
      proc_path = f'/proc/asound/card{card_id}/pcm{dev_id}p/sub0/status'
      script_parts.append(f'echo "===PLAY_{dev_id}==="')
      script_parts.append(f'cat {proc_path} 2>/dev/null || true')
    for dev_id in capture_devices:
      proc_path = f'/proc/asound/card{card_id}/pcm{dev_id}c/sub0/status'
      script_parts.append(f'echo "===CAP_{dev_id}==="')
      script_parts.append(f'cat {proc_path} 2>/dev/null || true')

    combined_out = self._shell(
        f"su 0 sh -c '{'; '.join(script_parts)}' || true"
    )
    sections: dict[str, list[str]] = {}
    current_key = ''
    for line in combined_out.splitlines():
      stripped = line.strip()
      if stripped.startswith('===') and stripped.endswith('==='):
        current_key = stripped[3:-3]
        sections[current_key] = []
      elif current_key:
        sections[current_key].append(line)

    hal_fds = '\n'.join(sections.get('HAL_FDS', ()))
    playback_running: dict[int, bool] = {}
    for dev_id in playback_devices:
      dev_node = f'/dev/snd/pcmC{card_id}D{dev_id}p'
      status = '\n'.join(sections.get(f'PLAY_{dev_id}', ()))
      playback_running[dev_id] = (
          'state: RUNNING' in status or dev_node in hal_fds
      )

    capture_running: dict[int, bool] = {}
    for dev_id in capture_devices:
      dev_node = f'/dev/snd/pcmC{card_id}D{dev_id}c'
      status = '\n'.join(sections.get(f'CAP_{dev_id}', ()))
      capture_running[dev_id] = (
          'state: RUNNING' in status or dev_node in hal_fds
      )

    return playback_running, capture_running

  def snapshot(
      self,
      playback_devices: Sequence[int] = (0, 1),
      capture_devices: Sequence[int] = (0,),
  ) -> AudioStackSnapshot:
    """Captures a correlated 5-layer snapshot across AudioService, APM, AF, and ALSA."""
    audio_raw = self._shell_bytes('dumpsys audio --proto')
    policy_dump = self._shell('dumpsys media.audio_policy')
    flinger_dump = self._shell('dumpsys media.audio_flinger')
    alsa_play, alsa_cap = self.read_alsa_running_states(
        playback_devices=playback_devices,
        capture_devices=capture_devices,
    )

    if audio_raw.lstrip().startswith(b'AudioService Dumpsys'):
      layer1 = parse_dumpsys_audio(audio_raw.decode('utf-8', errors='replace'))
    else:
      layer1 = parse_dumpsys_audio_proto(audio_raw)
    strategies, patches = parse_dumpsys_audio_policy(policy_dump)
    output_threads, input_threads = parse_dumpsys_audio_flinger(flinger_dump)

    return AudioStackSnapshot(
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
        output_threads=output_threads,
        input_threads=input_threads,
        alsa_playback_running=alsa_play,
        alsa_capture_running=alsa_cap,
    )

  def find_route_mismatches(
      self,
      snap: AudioStackSnapshot,
      strategy_name: str,
      expected_device_token: str,
      expected_address: str,
      expected_alsa_dev: int | None = None,
      inactive_alsa_devs: Sequence[int] = (),
      expected_mode: str | None = None,
  ) -> list[str]:
    """Returns descriptions of any layers that do not match the expected route."""
    mismatches: list[str] = []
    if expected_mode is not None:
      if snap.actual_mode != expected_mode:
        mismatches.append(
            f'Layer 1 (AudioService): expected Actual mode={expected_mode},'
            f' got {snap.actual_mode!r}'
        )
      if expected_mode != 'MODE_NORMAL':
        if not any(
            not s.paused
            and not s.client_paused
            and not s.closed
            and s.mode == expected_mode
            and device_matches(
                route_output(s.target_route),
                expected_device_token,
                expected_address,
            )
            for s in snap.mode_sessions
        ):
          mismatches.append(
              f'Layer 1 (AudioService): expected live AudioModeSession in'
              f' {expected_mode} with output route matching'
              f' {expected_device_token} ({expected_address!r}), got'
              f' {snap.mode_sessions}'
          )
        if not device_matches(
            snap.applied_preferred_comm_device,
            expected_device_token,
            expected_address,
        ):
          mismatches.append(
              'Layer 1 (AudioDeviceBroker):'
              ' appliedPreferredCommunicationDevice'
              f' {snap.applied_preferred_comm_device!r} != expected'
              f' {expected_device_token} ({expected_address!r})'
          )

    strat = snap.strategies.get(strategy_name)
    if strat is None:
      mismatches.append(
          f'Layer 2 (AudioPolicy): missing {strategy_name} in'
          f' {list(snap.strategies.keys())}'
      )
    else:
      if expected_device_token not in strat.device_types:
        mismatches.append(
            f'Layer 2 (AudioPolicy): expected {expected_device_token} in'
            f' {strategy_name} device_types, got {strat}'
        )
      if expected_address and not address_matches(
          strat.address, expected_address
      ):
        mismatches.append(
            f'Layer 2 (AudioPolicy): expected {strategy_name} address'
            f' {expected_address!r}, got {strat.address!r}'
        )

    if not any(
        expected_device_token in p.sink_device
        and address_matches(p.sink_device, expected_address)
        for p in snap.patches
    ):
      mismatches.append(
          f'Layer 3 (AudioPatches): expected active patch to sink'
          f' {expected_device_token} ({expected_address!r}), got'
          f' patches={snap.patches}'
      )

    if not any(
        not t.standby and expected_device_token in t.output_devices
        for t in snap.output_threads.values()
    ):
      mismatches.append(
          f'Layer 4 (AudioFlinger): expected non-standby output thread on'
          f' {expected_device_token}, got threads={snap.output_threads}'
      )

    if expected_alsa_dev is not None and not snap.alsa_playback_running.get(
        expected_alsa_dev, False
    ):
      mismatches.append(
          f'Layer 5 (ALSA): expected pcm{expected_alsa_dev}p to be RUNNING,'
          f' got {snap.alsa_playback_running}'
      )
    for dev in inactive_alsa_devs:
      if snap.alsa_playback_running.get(dev, True):
        mismatches.append(
            f'Layer 5 (ALSA): expected pcm{dev}p to be closed/inactive, got'
            f' {snap.alsa_playback_running}'
        )
    return mismatches

  def wait_for_route(
      self,
      strategy_name: str,
      expected_device_token: str,
      expected_address: str,
      expected_alsa_dev: int | None = None,
      inactive_alsa_devs: Sequence[int] = (),
      expected_mode: str | None = None,
      timeout_sec: float = DEFAULT_TIMEOUT_SEC,
      poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
  ) -> tuple[bool, AudioStackSnapshot]:
    """Polls until all 5 layers converge on the expected device route and ALSA stream."""
    deadline = time.monotonic() + timeout_sec
    last_snap: AudioStackSnapshot | None = None
    last_exc: ValueError | None = None
    while True:
      try:
        last_snap = self.snapshot()
        last_exc = None
      except ValueError as exc:
        last_exc = exc
      if (
          last_exc is None
          and last_snap is not None
          and not self.find_route_mismatches(
              last_snap,
              strategy_name=strategy_name,
              expected_device_token=expected_device_token,
              expected_address=expected_address,
              expected_alsa_dev=expected_alsa_dev,
              inactive_alsa_devs=inactive_alsa_devs,
              expected_mode=expected_mode,
          )
      ):
        return True, last_snap
      if time.monotonic() >= deadline:
        if last_snap is None and last_exc is not None:
          raise last_exc
        assert last_snap is not None
        return False, last_snap
      time.sleep(poll_interval_sec)

  def wait_for_frames_advancing(
      self,
      expected_device_token: str,
      timeout_sec: float = DEFAULT_TIMEOUT_SEC,
      poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
  ) -> tuple[int, int]:
    """Polls AudioFlinger until `Frames written` advances on the routed output thread."""
    deadline = time.monotonic() + timeout_sec
    baseline_frames = 0
    target_io_handle = None
    try:
      baseline_snap = self.snapshot()
      for io_handle, thread in baseline_snap.output_threads.items():
        if (
            not thread.standby
            and expected_device_token in thread.output_devices
        ):
          target_io_handle = io_handle
          baseline_frames = thread.frames_written
          break
    except ValueError:
      pass

    latest_frames = baseline_frames
    while time.monotonic() < deadline:
      time.sleep(poll_interval_sec)
      try:
        snap = self.snapshot()
      except ValueError:
        continue
      tracked = (
          snap.output_threads.get(target_io_handle)
          if target_io_handle is not None
          else None
      )
      if (
          tracked is not None
          and not tracked.standby
          and expected_device_token in tracked.output_devices
      ):
        latest_frames = tracked.frames_written
        if latest_frames > baseline_frames:
          return baseline_frames, latest_frames
      else:
        target_io_handle = None
        for io_handle, thread in snap.output_threads.items():
          if (
              not thread.standby
              and expected_device_token in thread.output_devices
          ):
            target_io_handle = io_handle
            baseline_frames = thread.frames_written
            latest_frames = baseline_frames
            break
    return baseline_frames, latest_frames
