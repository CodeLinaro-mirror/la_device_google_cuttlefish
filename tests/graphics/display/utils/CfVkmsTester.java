/*
 * Copyright (C) 2025 The Android Open Source Project
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

package com.android.cuttlefish.tests;

import com.android.tradefed.device.DeviceNotAvailableException;
import com.android.tradefed.device.ITestDevice;
import com.android.tradefed.log.LogUtil.CLog;
import com.android.tradefed.util.CommandResult;
import com.android.tradefed.util.CommandStatus;
import java.util.List;
import org.junit.Assert;

public class CfVkmsTester implements AutoCloseable {

  public static final long DISPLAY_BRINGUP_TIMEOUT_MS = 30000;

  private static final String CMD_SETUP = "setup";
  private static final String CMD_HOTPLUG = "hotplug";
  private static final String CMD_TEARDOWN = "reset";
  public static final long POLL_INTERVAL_MS = 1000;

  public enum Monitor {
    // eDP
    REDRIX,

    // DP (DisplayPort)
    HP_SPECTRE32_4K_DP,
    DEL_61463_DELL_U2410_DP,
    ACI_9713_ASUS_VE258_DP,

    // HDMI
    ACI_9155_ASUS_VH238_HDMI,
    HWP_12447_HP_Z24i_HDMI;
  }

  public static class VkmsConnectorSetup {
    private final Monitor monitor;
    private final int additionalOverlayPlanes;
    private final boolean enabledAtStart;

    private VkmsConnectorSetup(Monitor monitor, int additionalOverlayPlanes, boolean enabledAtStart) {
      this.monitor = monitor;
      this.additionalOverlayPlanes = additionalOverlayPlanes;
      this.enabledAtStart = enabledAtStart;
    }

    public Monitor getMonitor() { return monitor; }
    public int getAdditionalOverlayPlanes() { return additionalOverlayPlanes; }
    public boolean isEnabledAtStart() { return enabledAtStart; }

    public String buildScreenString() {
      if (additionalOverlayPlanes > 0) {
        return String.format("name=%s,planes=%d,enabled=%b",
            monitor.name(), additionalOverlayPlanes, enabledAtStart);
      } else {
        return String.format("name=%s,enabled=%b",
            monitor.name(), enabledAtStart);
      }
    }

    public static Builder builder() {
      return new Builder();
    }

    public static class Builder {
      private Monitor monitor = Monitor.HP_SPECTRE32_4K_DP;
      private int additionalOverlayPlanes = 0;
      private boolean enabledAtStart = true;

      public Builder setMonitor(Monitor monitor) {
        this.monitor = monitor;
        return this;
      }

      public Builder setAdditionalOverlayPlanes(int planes) {
        this.additionalOverlayPlanes = planes;
        return this;
      }

      public Builder setEnabledAtStart(boolean enabled) {
        this.enabledAtStart = enabled;
        return this;
      }

      public VkmsConnectorSetup build() {
        return new VkmsConnectorSetup(monitor, additionalOverlayPlanes, enabledAtStart);
      }
    }
  }

  private final ITestDevice device;
  private boolean initialized = false;

  private CfVkmsTester(ITestDevice device) {
    this.device = device;
  }

  public static CfVkmsTester createWithConfig(ITestDevice device, List<VkmsConnectorSetup> configs)
      throws DeviceNotAvailableException {
    device.enableAdbRoot();
    CfVkmsTester tester = new CfVkmsTester(device);
    StringBuilder configStr = new StringBuilder();
    for (int i = 0; i < configs.size(); i++) {
        if (i > 0) configStr.append(" ");
        configStr.append("--screen=").append(configs.get(i).buildScreenString());
    }

    if (tester.setup(configStr.toString())) {
      tester.initialized = true;
      return tester;
    }
    return null;
  }

  private boolean setup(String config) {
    try {
      String cmd =
          String.format("vkms_controller %s %s", CMD_SETUP, config);
      boolean success = false;
      for (int i = 0; i < 3; i++) {
        CommandResult result = device.executeShellV2Command(cmd);
        if (result.getStatus() == CommandStatus.SUCCESS && result.getExitCode() != null && result.getExitCode() == 0) {
          success = true;
          break;
        }
        CLog.w(
            "Try %d: Failed to setup VKMS via vkms_controller (status=%s, exitCode=%s): stdout=%s, stderr=%s",
            i + 1,
            result.getStatus(),
            result.getExitCode(),
            result.getStdout(),
            result.getStderr());
        Thread.sleep(3000);
      }

      if (!success) {
        CLog.e("Failed to setup VKMS via vkms_controller after 3 retries");
        return false;
      }

      return true;
    } catch (Exception e) {
      CLog.e("Exception during VKMS setup: %s", e.toString());
      return false;
    }
  }

  public void setConnectorStatus(int connectorIndex, boolean isConnected) {
    if (!initialized) {
      throw new RuntimeException("Tester is not initialized.");
    }
    try {
      String cmd =
          String.format(
              "vkms_controller %s %d %s",
              CMD_HOTPLUG, connectorIndex, isConnected ? "connected" : "disconnected");
      CommandResult result = device.executeShellV2Command(cmd);
      if (result.getStatus() != CommandStatus.SUCCESS
          || result.getExitCode() == null
          || result.getExitCode() != 0) {
        Assert.fail(
            String.format(
                "Failed to hotplug connector %d. Status: %s, Exit Code: %s, Stderr: %s, Stdout: %s",
                connectorIndex,
                result.getStatus(),
                result.getExitCode(),
                result.getStderr(),
                result.getStdout()));
      }
    } catch (Exception e) {
      Assert.fail("Exception during VKMS hotplug: " + e.toString());
    }
  }

  @Override
  public void close() throws Exception {
    tearDown();
  }

  public void tearDown() {
    if (!initialized) {
      return;
    }
    try {
      String cmd = String.format("vkms_controller %s", CMD_TEARDOWN);
      for (int i = 0; i < 3; i++) {
        CommandResult result = device.executeShellV2Command(cmd);
        if (result.getStatus() == CommandStatus.SUCCESS
            && result.getExitCode() != null
            && result.getExitCode() == 0) {
          break;
        }
        CLog.w(
            "Try %d: vkms_controller reset failed (status=%s, exitCode=%s): stdout=%s, stderr=%s",
            i + 1,
            result.getStatus(),
            result.getExitCode(),
            result.getStdout(),
            result.getStderr());
        Thread.sleep(2000);
      }

      // Wait for UI to recover after reset
      try {
        waitForUiReady(DISPLAY_BRINGUP_TIMEOUT_MS);
      } catch (Exception e) {
        CLog.w("UI failed to become ready after teardown: %s", e.getMessage());
      }
    } catch (Exception e) {
      CLog.e("Exception during VKMS teardown: %s", e.toString());
    } finally {
      initialized = false;
    }
  }

  public void waitForDisplaysToBeOn(int minimumExpectedDisplays, long waitTimeoutMs)
      throws Exception {
    long startTime = System.currentTimeMillis();
    int displayCount = 0;
    while (displayCount < minimumExpectedDisplays
        && System.currentTimeMillis() - startTime < waitTimeoutMs) {
      String command = "dumpsys SurfaceFlinger --displays | grep -c '^Display '";
      CommandResult result = device.executeShellV2Command(command);
      if (result.getStatus() == CommandStatus.SUCCESS) {
        try {
          displayCount = Integer.parseInt(result.getStdout().trim());
        } catch (NumberFormatException e) {
          displayCount = 0;
        }
      }
      if (displayCount < minimumExpectedDisplays) {
        Thread.sleep(POLL_INTERVAL_MS);
      }
    }
    if (displayCount < minimumExpectedDisplays) {
      throw new Exception(
          "Displays were not detected in time. Expected at least "
              + minimumExpectedDisplays
              + ", found "
              + displayCount);
    }
  }

  public void waitForUiReady(long timeoutMs) throws Exception {
    long startTime = System.currentTimeMillis();
    boolean ready = false;
    while (System.currentTimeMillis() - startTime < timeoutMs) {
      CommandResult result = device.executeShellV2Command("dumpsys window");
      if (result.getStatus() == CommandStatus.SUCCESS && result.getStdout() != null) {
        String stdout = result.getStdout();
        if (stdout.contains("mCurrentFocus=Window{")) {
          ready = true;
          break;
        }
      }
      Thread.sleep(POLL_INTERVAL_MS);
    }
    if (!ready) {
      throw new Exception("Timed out waiting for UI stack to become ready (window focus active)");
    }
  }
}
