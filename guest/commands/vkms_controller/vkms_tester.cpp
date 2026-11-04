/*
 * Copyright (C) 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "vkms_tester.h"  // NOLINT(build/include_subdir)

#include <android-base/file.h>
#include <android-base/properties.h>
#include <android-base/scopeguard.h>
#include <android-base/strings.h>
#include <android-base/unique_fd.h>
#include <cutils/properties.h>
#include <errno.h>
#include <inttypes.h>
#include <log/log.h>
#include <sys/wait.h>
#include <unistd.h>
#include <system_error>

#include <cassert>
#include <chrono>  // NOLINT(build/c++11)
#include <cstdio>
#include <cstdlib>
#include <filesystem>  // NOLINT(build/c++17)
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <thread>  // NOLINT(build/c++11)
#include <unordered_map>
#include <vector>

namespace cuttlefish {
namespace vkms_controller {

const std::unordered_map<VkmsTester::DrmResource, std::string_view>
    VkmsTester::kDrmResourceBase = {
        {DrmResource::kConnector, "connectors/CONNECTOR_"},
        {DrmResource::kCrtc, "crtcs/CRTC_"},
        {DrmResource::kEncoder, "encoders/ENCODER_"},
        {DrmResource::kPlane, "planes/PLANE_"},
};

namespace {
// `/config/vkms` is the base directory for VKMS in ConfigFS. `my-vkms` is the
// chosen name of the VKMS instance which can be anything.
const std::filesystem::path kVkmsBaseDir = "/config/vkms/my-vkms";

constexpr auto kDrmPollInterval = std::chrono::milliseconds(50);

// https://cs.android.com/android/platform/superproject/main/+/main:external/libdrm/xf86drmMode.h;l=190
enum class ConnectorStatus {
  kConnected = 1,
  kDisconnected = 2,
  kUnknown = 3,
};

// Create a map of the base directory for each resource type to maintain
bool WriteResourceConfig(std::string_view resourceBase, int index,
                         const std::string& node, const std::string& value) {
  std::filesystem::path dir =
      std::filesystem::path(kVkmsBaseDir) /
      (std::string(resourceBase) + std::to_string(index));
  std::filesystem::path path = dir / node;
  if (!android::base::WriteStringToFile(value, path.string())) {
    ALOGE("Failed to write '%s' to %s: %s", value.c_str(),
          path.string().c_str(), strerror(errno));
    return false;
  }
  return true;
}

bool LinkResources(std::string_view srcResourceBase, int srcIdx,
                   std::string_view targetResourceBase, int targetIdx) {
  std::string targetName =
      std::string(targetResourceBase) + std::to_string(targetIdx);
  std::filesystem::path srcDir =
      std::filesystem::path(kVkmsBaseDir) /
      (std::string(srcResourceBase) + std::to_string(srcIdx));
  std::filesystem::path symlinkPath = srcDir / ("possible_" + targetName);
  std::filesystem::path targetDir =
      std::filesystem::path(kVkmsBaseDir) / targetName;

  std::error_code ec;
  std::filesystem::create_symlink(targetDir, symlinkPath, ec);
  if (ec && ec != std::errc::file_exists) {
    ALOGE("Failed to create symlink at %s pointing to %s: %s",
          symlinkPath.string().c_str(), targetDir.string().c_str(),
          ec.message().c_str());
    return false;
  }
  return true;
}

bool WaitForDeviceNode(const std::string& path, bool expect_exists) {
  // Wait up to 15 seconds (300 * 50ms) for the device node
  for (int i = 0; i < 300; i++) {
    bool exists = (access(path.c_str(), F_OK) == 0);
    if (exists == expect_exists) {
      return true;
    }
    std::this_thread::sleep_for(kDrmPollInterval);
  }
  return false;
}

}  // namespace

// static
std::unique_ptr<VkmsTester> VkmsTester::CreateWithBuilders(
    const std::vector<VkmsConnectorBuilder>& builders) {
  if (builders.empty()) {
    ALOGE(
        "Empty configuration provided. At least one connector must be "
        "specified.");
    return nullptr;
  }

  auto tester =
      std::unique_ptr<VkmsTester>(new VkmsTester(builders.size(), builders));

  if (!tester->mInitialized) {
    ALOGE("Failed to initialize VkmsTester with Builder Config");
    return nullptr;
  }

  return tester;
}

// static
std::unique_ptr<VkmsTester> VkmsTester::CreateWithGenericConnectors(
    int displaysCount) {
  if (displaysCount <= 0) {
    ALOGE("Invalid number of displays: %d", displaysCount);
    return nullptr;
  }

  auto tester = std::unique_ptr<VkmsTester>(new VkmsTester(displaysCount));

  if (!tester->mInitialized) {
    ALOGE("Failed to initialize VkmsTester with Generic Config");
    return nullptr;
  }

  return tester;
}

VkmsTester::VkmsTester(size_t displaysCount,
                       const std::vector<VkmsConnectorBuilder>& builders) {
  auto display_guard = StopDisplayStack();
  if (!display_guard) {
    ALOGE("Failed to safely stop display stack; aborting VKMS setup.");
    return;
  }

  CleanUpConfigFs();

  std::unordered_set<std::string> existing_cards = GetExistingDrmDevices();

  bool vkms_setup_success = false;
  auto cleanup_guard =
      android::base::make_scope_guard([&vkms_setup_success, this]() {
        if (!vkms_setup_success) {
          CleanUpConfigFs();
          ToggleVkmsAsDisplayDriver(false);
        }
      });

  if (!ToggleVkmsAsDisplayDriver(true) ||
      !SetupDisplays(displaysCount, builders) || !ToggleVkms(true)) {
    ALOGE("Failed to set up VKMS");
    return;
  }

  std::optional<std::string> new_card_opt = WaitForNewDrmDevice(existing_cards);
  if (!new_card_opt.has_value()) {
    ALOGE("Timed out waiting for new VKMS DRM card to appear");
    return;
  }

  std::string new_card = std::move(new_card_opt.value());
  if (property_set("vendor.hwc.drm.device", new_card.c_str()) != 0 ||
      !android::base::WaitForProperty("vendor.hwc.drm.device", new_card,
                                      std::chrono::milliseconds(5000))) {
    ALOGE("Failed to set vendor.hwc.drm.device property to %s",
          new_card.c_str());
    return;
  }
  ALOGI("Successfully detected and set vendor.hwc.drm.device to %s",
        new_card.c_str());

  vkms_setup_success = true;

  if (!display_guard->Start()) {
    ALOGE("Failed to start display stack after successful VKMS setup");
    vkms_setup_success = false;
  }

  if (vkms_setup_success) {
    this->mInitialized = true;
  }
}

// static
std::unordered_set<std::string> VkmsTester::GetExistingDrmDevices() {
  std::unordered_set<std::string> devices;
  std::error_code ec;
  auto it = std::filesystem::directory_iterator("/dev/dri/", ec);
  if (ec) {
    ALOGW("Failed to list /dev/dri/: %s", ec.message().c_str());
    return devices;
  }

  const auto end = std::filesystem::directory_iterator();
  while (it != end && !ec) {
    const auto& entry = *it;
    if (entry.is_character_file(ec)) {
      std::string filename = entry.path().filename().string();
      if (android::base::StartsWith(filename, "card")) {
        devices.insert(std::move(filename));
      }
    }
    if (ec) {
      if (ec != std::errc::no_such_file_or_directory) {
        ALOGW("Failed to read character file status for %s: %s",
              entry.path().string().c_str(), ec.message().c_str());
      }
      ec.clear();
    }
    it.increment(ec);
  }
  return devices;
}

// static
std::optional<std::string> VkmsTester::WaitForNewDrmDevice(
    const std::unordered_set<std::string>& existing_devices,
    std::chrono::milliseconds timeout) {
  auto start = std::chrono::steady_clock::now();

  while (std::chrono::steady_clock::now() - start < timeout) {
    std::unordered_set<std::string> current_devices = GetExistingDrmDevices();
    for (const auto& device : current_devices) {
      if (existing_devices.find(device) == existing_devices.end()) {
        return "/dev/dri/" + device;
      }
    }
    std::this_thread::sleep_for(kDrmPollInterval);
  }
  return std::nullopt;
}

VkmsTester::~VkmsTester() {
  if (mOwnsResources && mInitialized) {
    ShutdownAndCleanUpVkms();
  }
}

bool VkmsTester::ToggleConnector(int connectorIndex, bool enable) {
  return SetConnectorStatus(connectorIndex, enable);
}

// static
bool VkmsTester::ToggleVkmsAsDisplayDriver(bool enable) {
  // On Disabling VKMS, we don't need to do anything else.
  if (!enable) {
    if (property_set("vendor.hwc.drm.device", "/dev/dri/card0") != 0 ||
        !android::base::WaitForProperty("vendor.hwc.drm.device",
                                        "/dev/dri/card0",
                                        std::chrono::milliseconds(5000))) {
      ALOGE("Failed to set vendor.hwc.drm.device property to /dev/dri/card0");
      return false;
    }
    return true;
  }

  // Create VKMS directory if we're enabling VKMS.
  std::error_code ec;
  std::filesystem::create_directories(kVkmsBaseDir, ec);
  if (ec) {
    ALOGE("Failed to create directory %s: %s", kVkmsBaseDir.string().c_str(),
          ec.message().c_str());
    return false;
  }

  ALOGI("Successfully created directory %s or already exists",
        kVkmsBaseDir.string().c_str());
  return true;
}

bool VkmsTester::SetupDisplays(
    int displaysCount, const std::vector<VkmsConnectorBuilder>& builders) {
  std::vector<VkmsConnectorBuilder> effectiveBuilders = builders;
  if (effectiveBuilders.empty()) {
    for (int i = 0; i < displaysCount; ++i) {
      effectiveBuilders.push_back(
          VkmsConnectorBuilder::create()
              .withType(i == 0 ? ConnectorType::keDP
                               : ConnectorType::kDisplayPort)
              .enabledAtStart(false));
    }
  }

  if (displaysCount != static_cast<int>(effectiveBuilders.size())) {
    ALOGE("Mismatch between requested displays count and builder config size");
    return false;
  }

  for (int i = 0; i < displaysCount; ++i) {
    const auto& builder = effectiveBuilders[i];
    bool success = CreateResource(DrmResource::kCrtc, i) &&
                   SetCrtcWriteback(i, true) &&
                   CreateResource(DrmResource::kEncoder, i) &&
                   LinkToCrtc(DrmResource::kEncoder, i, i) &&
                   CreateResource(DrmResource::kConnector, i) &&
                   SetConnectorStatus(i, builder.mEnabledAtStart) &&
                   SetConnectorType(i, builder.mType);

    if (!success) {
      return false;
    }

    if (builder.mMonitorName.type != MonitorName::Type::UNSET) {
      if (!SetConnectorEdid(i, builder.mMonitorName)) {
        return false;
      }
    }

    if (!LinkConnectorToEncoder(i, i)) {
      return false;
    }

    std::vector<PlaneType> planes = {PlaneType::kCursor, PlaneType::kPrimary};
    for (int j = 0; j < builder.mAdditionalOverlayPlanes; ++j) {
      planes.push_back(PlaneType::kOverlay);
    }

    for (auto type : planes) {
      success = CreateResource(DrmResource::kPlane, mLatestPlaneId) &&
                SetPlaneType(mLatestPlaneId, type) &&
                SetPlaneFormat(mLatestPlaneId, type) &&
                LinkToCrtc(DrmResource::kPlane, mLatestPlaneId, i);

      if (!success) {
        return false;
      }
      mLatestPlaneId++;
    }

    ALOGI("Successfully set up display %i", i);
  }

  return true;
}

// static
bool VkmsTester::ToggleVkms(bool enable) {
  std::filesystem::path path = std::filesystem::path(kVkmsBaseDir) / "enabled";
  std::string value = enable ? "1" : "0";
  // Retry writing if it fails with ENOENT, it might be due to async kernel
  // cleanup of the old device
  int saved_errno = 0;
  for (int i = 0; i < 10; ++i) {
    if (android::base::WriteStringToFile(value, path.string())) {
      ALOGI("Successfully toggled VKMS at %s", path.string().c_str());
      return true;
    }
    saved_errno = errno;
    if (saved_errno != ENOENT) {
      break;
    }
    ALOGW("enabled file not found, retrying in 100ms... (try %d)", i + 1);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  ALOGE("Failed to toggle VKMS: %s", strerror(saved_errno));
  return false;
}

// static
std::optional<VkmsTester::DisplayStackGuard> VkmsTester::StopDisplayStack() {
  // We must stop the display stack before reconfiguring VKMS.
  // The correct order for stopping is reverse-dependency:
  // Zygote -> Boot Animation -> SurfaceFlinger -> HWC.
  //
  // CRITICAL: We dynamically track which services were actually 'running'
  // before we stopped them. This prevents us from unconditionally restarting
  // services (like surfaceflinger) during StartDisplayStack if an external
  // test harness (e.g. Tradefed for TestHwcComposition) deliberately stopped
  // them prior to invoking vkms_controller.
  //
  // We must also stop zygote and zygote_secondary before surfaceflinger.
  // Why? If we stop surfaceflinger while system_server (a child of zygote)
  // is running, system_server will detect the dead Binder connection and
  // intentionally crash itself. Android's 'init' daemon is hardcoded to
  // instantly restart zygote to recover from a system_server crash. This
  // triggers an uncontrolled restart cascade that races with our vkms_tester
  // configuration, resulting in system instability and flaky tests. By
  // gracefully stopping zygote first, we safely drain the UI framework stack.
  //
  // NOTE: We do not kill `vendor.graphics.allocator`. Native host tests (e.g.
  // VkmsTestHwcWriteback) run multiple test cases in a single process.
  // Bouncing the allocator breaks libui's static Binder cache, causing "-129
  // EX_TRANSACTION_FAILED" on subsequent tests.
  std::vector<std::string> services = {"zygote_secondary", "zygote", "bootanim",
                                       "surfaceflinger", "vendor.hwcomposer-3"};
  std::vector<std::string> services_to_restart;

  for (const auto& service : services) {
    std::string state =
        android::base::GetProperty("init.svc." + service, "not_found");
    if (state == "not_found") {
      ALOGI("Service %s not found, skipping", service.c_str());
      continue;
    }
    bool was_running = (state == "running" || state == "restarting");

    if (property_set("ctl.stop", service.c_str()) != 0) {
      ALOGE("Failed to set property ctl.stop to %s", service.c_str());
      std::reverse(services_to_restart.begin(), services_to_restart.end());
      StartDisplayStack(services_to_restart);
      return std::nullopt;
    }
    if (!android::base::WaitForProperty("init.svc." + service, "stopped",
                                        std::chrono::seconds(15))) {
      ALOGE("Timed out waiting for %s to stop", service.c_str());
      std::reverse(services_to_restart.begin(), services_to_restart.end());
      StartDisplayStack(services_to_restart);
      return std::nullopt;
    }

    if (was_running) {
      services_to_restart.push_back(service);
    }
    ALOGI("Successfully stopped %s", service.c_str());
  }

  // Reverse to get the correct start order: hwc -> sf -> bootanim
  std::reverse(services_to_restart.begin(), services_to_restart.end());

  return DisplayStackGuard(std::move(services_to_restart));
}

// static
bool VkmsTester::StartDisplayStack(const std::vector<std::string>& services) {
  // Wait for the target DRM device to become available to prevent race
  // conditions where surfaceflinger/hwcomposer start before udev creates the
  // device node.
  std::string drm_device =
      android::base::GetProperty("vendor.hwc.drm.device", "/dev/dri/card0");
  if (!WaitForDeviceNode(drm_device, true)) {
    ALOGE("Timed out waiting for DRM device %s to appear", drm_device.c_str());
    return false;
  }

  // Only restart the services that were actively running before we called
  // StopDisplayStack. This preserves the expected state for external test
  // harnesses.
  for (const auto& service : services) {
    if (property_set("ctl.start", service.c_str()) != 0) {
      ALOGE("Failed to set property ctl.start to %s", service.c_str());
      return false;
    }
    if (!android::base::WaitForProperty("init.svc." + service, "running",
                                        std::chrono::seconds(15))) {
      ALOGE("Timed out waiting for %s to start", service.c_str());
      return false;
    }
    ALOGI("Successfully started %s", service.c_str());
  }
  return true;
}

bool VkmsTester::CreateResource(DrmResource resource, int index) {
  std::string resourceBase = std::string(kDrmResourceBase.at(resource));
  std::filesystem::path resourceDir = std::filesystem::path(kVkmsBaseDir) /
                                      (resourceBase + std::to_string(index));
  std::error_code ec;
  std::filesystem::create_directories(resourceDir, ec);
  if (ec) {
    ALOGE("Failed to create directory %s: %s", resourceDir.string().c_str(),
          ec.message().c_str());
    return false;
  }

  return true;
}

bool VkmsTester::SetCrtcWriteback(int crtcIndex, bool enable) {
  bool success =
      WriteResourceConfig(kDrmResourceBase.at(DrmResource::kCrtc), crtcIndex,
                          "writeback", enable ? "1" : "0");
  if (success) {
    ALOGI("Successfully toggled writeback for CRTC %i: %s", crtcIndex,
          enable ? "enabled" : "disabled");
  }
  return success;
}

bool VkmsTester::SetConnectorStatus(int index, bool enable) {
  ConnectorStatus status =
      enable ? ConnectorStatus::kConnected : ConnectorStatus::kDisconnected;
  bool success =
      WriteResourceConfig(kDrmResourceBase.at(DrmResource::kConnector), index,
                          "status", std::to_string(static_cast<int>(status)));
  if (success) {
    ALOGI("Successfully toggled connector %i: %s", index,
          enable ? "connected" : "disconnected");
  }
  return success;
}

bool VkmsTester::SetConnectorType(int index, ConnectorType type) {
  bool success =
      WriteResourceConfig(kDrmResourceBase.at(DrmResource::kConnector), index,
                          "type", std::to_string(static_cast<int>(type)));
  if (success) {
    ALOGI("Successfully set connector %i type to %i", index,
          static_cast<int>(type));
  }
  return success;
}

bool VkmsTester::SetConnectorEdid(int index, MonitorName monitorName) {
  std::vector<uint8_t> edidData = getBinaryEdidForMonitor(monitorName);
  if (edidData.empty()) {
    ALOGE("Failed to get EDID data for monitor");
    return false;
  }

  std::filesystem::path connectorDir =
      std::filesystem::path(kVkmsBaseDir) /
      (std::string(kDrmResourceBase.at(DrmResource::kConnector)) +
       std::to_string(index));
  std::filesystem::path edidPath = connectorDir / "edid";

  android::base::unique_fd fd(
      open(edidPath.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644));
  if (fd.get() == -1) {
    ALOGE("Failed to open EDID file for writing: %s", strerror(errno));
    return false;
  }

  bool success =
      android::base::WriteFully(fd.get(), edidData.data(), edidData.size());

  if (success) {
    ALOGI("Successfully wrote EDID data with size %" PRIu64 " to connector %i",
          static_cast<uint64_t>(edidData.size()), index);
  } else {
    ALOGE("Failed to write complete EDID data: %s", strerror(errno));
  }

  return success;
}

bool VkmsTester::SetPlaneType(int index, PlaneType type) {
  bool success =
      WriteResourceConfig(kDrmResourceBase.at(DrmResource::kPlane), index,
                          "type", std::to_string(static_cast<int>(type)));
  if (success) {
    ALOGI("Successfully set plane %i type to %i", index,
          static_cast<int>(type));
  }
  return success;
}

bool VkmsTester::SetPlaneFormat(int index, PlaneType type) {
  std::string formats;
  if (type == PlaneType::kCursor) {
    formats = "ARGB8888";
  } else {
    formats = "AR24,AB24,XR24,XB24,RG16";
  }
  bool success = WriteResourceConfig(kDrmResourceBase.at(DrmResource::kPlane),
                                     index, "supported_formats", formats);
  if (success) {
    ALOGI("Successfully set plane %i format", index);
  }
  return success;
}

bool VkmsTester::LinkToCrtc(DrmResource resource, int resourceIdx,
                            int crtcIdx) {
  bool success =
      LinkResources(kDrmResourceBase.at(resource), resourceIdx,
                    kDrmResourceBase.at(DrmResource::kCrtc), crtcIdx);
  if (success) {
    ALOGI("Successfully linked resource %i to CRTC %i", resourceIdx, crtcIdx);
  }
  return success;
}

bool VkmsTester::LinkConnectorToEncoder(int connectorIdx, int encoderIdx) {
  bool success =
      LinkResources(kDrmResourceBase.at(DrmResource::kConnector), connectorIdx,
                    kDrmResourceBase.at(DrmResource::kEncoder), encoderIdx);
  if (success) {
    ALOGI("Successfully linked connector %i to encoder %i", connectorIdx,
          encoderIdx);
  }
  return success;
}

// static
void VkmsTester::CleanUpConfigFs() {
  std::error_code ec;
  if (!std::filesystem::exists(kVkmsBaseDir, ec)) {
    return;
  }
  if (!ToggleVkms(false)) {
    ALOGE("Kernel refused to disable VKMS. Aborting ConfigFS manual teardown.");
    return;
  }

  // Wait for the DRM device to be removed by ueventd to prevent the next test
  // from incorrectly finding the old node before it is deleted.
  // We only wait if a dynamic VKMS card was actually set and is not the
  // persistent primary GPU card0.
  std::string drm_device =
      android::base::GetProperty("vendor.hwc.drm.device", "");
  if (!drm_device.empty() && drm_device != "/dev/dri/card0") {
    if (!WaitForDeviceNode(drm_device, false)) {
      ALOGW("Timed out waiting for DRM device %s to disappear",
            drm_device.c_str());
    }
  }

  // Clean up manually created relationships first under
  // possible_(crtcs/encoders). This is required before we started cleaning up
  // the directories.
  FindAndCleanupPossibleLinks(kVkmsBaseDir);
  CleanUpDirAndChildren(kVkmsBaseDir);
}

// static
// ConfigFS has special rules about deletion, so we need to clean up manually
// every layer.
void VkmsTester::ShutdownAndCleanUpVkms() {
  auto display_guard = StopDisplayStack();
  if (!display_guard) {
    ALOGE("Failed to safely stop display stack; aborting VKMS cleanup.");
    return;
  }

  CleanUpConfigFs();
  ToggleVkmsAsDisplayDriver(false);

  if (!display_guard->Start()) {
    ALOGE("Failed to restart display stack during shutdown");
  }
}

// static
void VkmsTester::FindAndCleanupPossibleLinks(const std::string& dirPath) {
  std::error_code ec;
  if (!std::filesystem::exists(dirPath, ec) ||
      !std::filesystem::is_directory(dirPath, ec)) {
    return;
  }

  auto it = std::filesystem::directory_iterator(dirPath, ec);
  const auto end = std::filesystem::directory_iterator();
  while (it != end && !ec) {
    const auto& entry = *it;
    if (entry.is_directory(ec)) {
      std::string dirname = entry.path().filename().string();
      // If this is a "possible_*" directory, process it specially
      if (dirname.find("possible_") == 0) {
        std::filesystem::remove_all(entry.path(), ec);
        if (ec) {
          ALOGW("Failed to remove_all on %s: %s", entry.path().string().c_str(),
                ec.message().c_str());
          ec.clear();
        }
      } else {
        FindAndCleanupPossibleLinks(entry.path().string());
      }
    } else if (ec) {
      ALOGW("Failed is_directory check for %s: %s",
            entry.path().string().c_str(), ec.message().c_str());
      ec.clear();
    }
    it.increment(ec);
    if (ec) {
      ALOGW("Error incrementing directory iterator in %s: %s", dirPath.c_str(),
            ec.message().c_str());
      ec.clear();
    }
  }
}

// static
void VkmsTester::CleanUpDirAndChildren(const std::string& dirPath) {
  std::error_code ec;
  if (!std::filesystem::exists(dirPath, ec) ||
      !std::filesystem::is_directory(dirPath, ec)) {
    return;
  }

  auto it = std::filesystem::directory_iterator(dirPath, ec);
  const auto end = std::filesystem::directory_iterator();
  while (it != end && !ec) {
    const auto& entry = *it;
    if (entry.is_directory(ec)) {
      CleanUpDirAndChildren(entry.path().string());
    } else if (ec) {
      ALOGW("Failed is_directory check for %s: %s",
            entry.path().string().c_str(), ec.message().c_str());
      ec.clear();
    }
    it.increment(ec);
    if (ec) {
      ALOGW("Error incrementing directory iterator in %s: %s", dirPath.c_str(),
            ec.message().c_str());
      ec.clear();
    }
  }
  int retries = 50;  // Try for up to 1 second (50 * 20ms)
  while (retries-- > 0) {
    std::filesystem::remove(dirPath, ec);
    if (!ec) {
      break;
    }
    if (ec == std::errc::device_or_resource_busy) {
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    } else {
      break;
    }
  }

  if (ec) {
    ALOGW("Failed to remove directory %s: %s", dirPath.c_str(),
          ec.message().c_str());
    ec.clear();
  }
}

}  // namespace vkms_controller
}  // namespace cuttlefish
