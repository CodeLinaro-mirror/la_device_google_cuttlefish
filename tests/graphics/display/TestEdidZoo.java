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

package com.android.cuttlefish.tests;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import com.android.tradefed.testtype.DeviceJUnit4ClassRunner;
import com.android.tradefed.testtype.junit4.BaseHostJUnit4Test;
import com.android.tradefed.util.CommandResult;
import com.android.tradefed.util.CommandStatus;
import com.android.tradefed.util.RunUtil;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * End-to-end VKMS ConfigFS tests verifying odd-but-legal monitor EDIDs across
 * drm_hwcomposer, SurfaceFlinger, and DisplayManager ("EDID Zoo").
 */
@RunWith(DeviceJUnit4ClassRunner.class)
public class TestEdidZoo extends BaseHostJUnit4Test {
    private static final String VKMS_CONNECTOR_1_EDID_PATH =
            "/config/vkms/my-vkms/connectors/CONNECTOR_1/edid";
    private static final int MIN_EXTERNAL_DENSITY_DPI = 100;
    private static final int MAX_EXTERNAL_DENSITY_DPI = 400;

    private CfVkmsTester mVkmsTester;

    @After
    public void tearDown() throws Exception {
        if (mVkmsTester != null) {
            mVkmsTester.close();
            mVkmsTester = null;
        }
    }

    private static class DtdSpec {
        final int pixelClockKhz;
        final int hActive;
        final int hBlank;
        final int hFrontPorch;
        final int hSyncWidth;
        final int vActive;
        final int vBlank;
        final int vFrontPorch;
        final int vSyncWidth;
        final int hSizeMm;
        final int vSizeMm;

        DtdSpec(int pixelClockKhz, int hActive, int hBlank, int hFrontPorch, int hSyncWidth,
                int vActive, int vBlank, int vFrontPorch, int vSyncWidth, int hSizeMm,
                int vSizeMm) {
            this.pixelClockKhz = pixelClockKhz;
            this.hActive = hActive;
            this.hBlank = hBlank;
            this.hFrontPorch = hFrontPorch;
            this.hSyncWidth = hSyncWidth;
            this.vActive = vActive;
            this.vBlank = vBlank;
            this.vFrontPorch = vFrontPorch;
            this.vSyncWidth = vSyncWidth;
            this.hSizeMm = hSizeMm;
            this.vSizeMm = vSizeMm;
        }
    }

    private static class ExternalDisplayState {
        int displayId = -1;
        int realWidth;
        int realHeight;
        int densityDpi;
        final List<String> modes = new ArrayList<>();
    }

    private static byte[] buildDtd(DtdSpec s) {
        byte[] b = new byte[18];
        int pclk10k = s.pixelClockKhz / 10;
        b[0] = (byte) (pclk10k & 0xFF);
        b[1] = (byte) ((pclk10k >> 8) & 0xFF);
        b[2] = (byte) (s.hActive & 0xFF);
        b[3] = (byte) (s.hBlank & 0xFF);
        b[4] = (byte) ((((s.hActive >> 8) & 0x0F) << 4) | ((s.hBlank >> 8) & 0x0F));
        b[5] = (byte) (s.vActive & 0xFF);
        b[6] = (byte) (s.vBlank & 0xFF);
        b[7] = (byte) ((((s.vActive >> 8) & 0x0F) << 4) | ((s.vBlank >> 8) & 0x0F));
        b[8] = (byte) (s.hFrontPorch & 0xFF);
        b[9] = (byte) (s.hSyncWidth & 0xFF);
        b[10] = (byte) ((((s.vFrontPorch) & 0x0F) << 4) | (s.vSyncWidth & 0x0F));
        b[11] = (byte) ((((s.hFrontPorch >> 8) & 0x03) << 6)
                | (((s.hSyncWidth >> 8) & 0x03) << 4)
                | (((s.vFrontPorch >> 4) & 0x03) << 2)
                | ((s.vSyncWidth >> 4) & 0x03));
        b[12] = (byte) (s.hSizeMm & 0xFF);
        b[13] = (byte) (s.vSizeMm & 0xFF);
        b[14] = (byte) ((((s.hSizeMm >> 8) & 0x0F) << 4) | ((s.vSizeMm >> 8) & 0x0F));
        b[15] = 0;
        b[16] = 0;
        b[17] = 0x1E;
        return b;
    }

    private static byte[] buildMonitorNameDescriptor(String name) {
        byte[] b = new byte[18];
        b[3] = (byte) 0xFC;
        byte[] ascii = name.getBytes(StandardCharsets.US_ASCII);
        int len = Math.min(ascii.length, 12);
        System.arraycopy(ascii, 0, b, 5, len);
        if (len < 13) {
            b[5 + len] = 0x0A;
            Arrays.fill(b, 5 + len + 1, 18, (byte) 0x20);
        }
        return b;
    }

    private static byte[] buildRangeDescriptor(int minVHz, int maxVHz, int maxPclkMhz) {
        byte[] b = new byte[18];
        b[3] = (byte) 0xFD;
        b[5] = (byte) minVHz;
        b[6] = (byte) maxVHz;
        b[7] = 15;
        b[8] = (byte) 250;
        b[9] = (byte) Math.max(1, (maxPclkMhz + 9) / 10);
        b[10] = 0x01;
        b[11] = 0x0A;
        Arrays.fill(b, 12, 18, (byte) 0x20);
        return b;
    }

    private static void applyChecksum(byte[] block, int offset) {
        int sum = 0;
        for (int i = 0; i < 127; i++) {
            sum = (sum + (block[offset + i] & 0xFF)) & 0xFF;
        }
        block[offset + 127] = (byte) ((256 - sum) & 0xFF);
    }

    private static byte[] buildEdid(int productCode, int serial, String name, int hSizeCm,
            int vSizeCm, int minVHz, int maxVHz, int maxPclkMhz, List<DtdSpec> dtds,
            byte[] ctaExtension) {
        byte[] base = new byte[128];
        base[0] = 0x00;
        Arrays.fill(base, 1, 7, (byte) 0xFF);
        base[7] = 0x00;
        // Manufacturer "GGL"
        int mfg = ((('G' - '@') & 0x1F) << 10) | ((('G' - '@') & 0x1F) << 5)
                | (('L' - '@') & 0x1F);
        base[8] = (byte) ((mfg >> 8) & 0xFF);
        base[9] = (byte) (mfg & 0xFF);
        base[10] = (byte) (productCode & 0xFF);
        base[11] = (byte) ((productCode >> 8) & 0xFF);
        base[12] = (byte) (serial & 0xFF);
        base[13] = (byte) ((serial >> 8) & 0xFF);
        base[14] = (byte) ((serial >> 16) & 0xFF);
        base[15] = (byte) ((serial >> 24) & 0xFF);
        base[16] = 1;
        base[17] = 35;
        base[18] = 1;
        base[19] = 4;
        base[20] = (byte) 0xA5;
        base[21] = (byte) (hSizeCm & 0xFF);
        base[22] = (byte) (vSizeCm & 0xFF);
        base[23] = 120;
        base[24] = 0x06;
        byte[] chrom = new byte[] {
                (byte) 0xEE, (byte) 0x91, (byte) 0xA3, 0x54, 0x4C,
                (byte) 0x99, 0x26, 0x0F, 0x50, 0x54};
        System.arraycopy(chrom, 0, base, 25, chrom.length);
        Arrays.fill(base, 38, 54, (byte) 0x01);

        List<byte[]> slots = new ArrayList<>();
        for (int i = 0; i < Math.min(dtds.size(), 2); i++) {
            slots.add(buildDtd(dtds.get(i)));
        }
        slots.add(buildRangeDescriptor(minVHz, maxVHz, maxPclkMhz));
        slots.add(buildMonitorNameDescriptor(name));
        while (slots.size() < 4) {
            byte[] dummy = new byte[18];
            dummy[3] = 0x10;
            slots.add(dummy);
        }
        for (int i = 0; i < 4; i++) {
            System.arraycopy(slots.get(i), 0, base, 54 + i * 18, 18);
        }
        base[126] = (byte) (ctaExtension != null ? 1 : 0);
        applyChecksum(base, 0);

        if (ctaExtension == null) {
            return base;
        }
        byte[] full = new byte[256];
        System.arraycopy(base, 0, full, 0, 128);
        System.arraycopy(ctaExtension, 0, full, 128, 128);
        applyChecksum(full, 128);
        return full;
    }

    private static byte[] buildCtaExtension(int[] vicList, List<DtdSpec> extraDtds,
            boolean includeHdrAndAudio) {
        byte[] cta = new byte[128];
        cta[0] = 0x02;
        cta[1] = 0x03;
        int pos = 4;
        if (vicList != null && vicList.length > 0) {
            int count = Math.min(vicList.length, 31);
            cta[pos++] = (byte) ((2 << 5) | count);
            for (int i = 0; i < count; i++) {
                cta[pos++] = (byte) (vicList[i] & 0x7F);
            }
        }
        if (includeHdrAndAudio) {
            // Audio Data Block (LPCM 2ch)
            cta[pos++] = (byte) ((1 << 5) | 3);
            cta[pos++] = 0x09;
            cta[pos++] = 0x07;
            cta[pos++] = 0x07;
            // Speaker Allocation Data Block
            cta[pos++] = (byte) ((4 << 5) | 3);
            cta[pos++] = 0x01;
            cta[pos++] = 0x00;
            cta[pos++] = 0x00;
            // HDMI 1.4 VSDB
            cta[pos++] = (byte) ((3 << 5) | 5);
            cta[pos++] = 0x03;
            cta[pos++] = 0x0C;
            cta[pos++] = 0x00;
            cta[pos++] = 0x10;
            cta[pos++] = 0x00;
            // Colorimetry Data Block (BT.2020 YCC + RGB)
            cta[pos++] = (byte) ((7 << 5) | 3);
            cta[pos++] = 0x05;
            cta[pos++] = (byte) 0xC0;
            cta[pos++] = 0x00;
            // HDR Static Metadata Data Block (SDR + HDR10 PQ + HLG)
            cta[pos++] = (byte) ((7 << 5) | 6);
            cta[pos++] = 0x06;
            cta[pos++] = 0x0D;
            cta[pos++] = 0x00;
            cta[pos++] = (byte) 128;
            cta[pos++] = (byte) 100;
            cta[pos++] = 20;
        }
        cta[2] = (byte) pos;
        int nativeDtdCount = extraDtds != null ? Math.min(extraDtds.size(), 0x0F) : 0;
        cta[3] = (byte) ((includeHdrAndAudio ? 0xF0 : 0x00) | nativeDtdCount);
        if (extraDtds != null) {
            for (DtdSpec spec : extraDtds) {
                if (pos + 18 <= 127) {
                    byte[] dtd = buildDtd(spec);
                    System.arraycopy(dtd, 0, cta, pos, 18);
                    pos += 18;
                }
            }
        }
        applyChecksum(cta, 0);
        return cta;
    }

    private static byte[] buildDisplayIdTiledExtension(int numHTiles, int numVTiles,
            int tileHLoc, int tileVLoc, int tileWidth, int tileHeight, String topologyId) {
        byte[] ext = new byte[128];
        ext[0] = (byte) 0x70; // DISPLAYID_EXT
        ext[1] = 0x12; // DisplayID v1.2
        // Section length (byte 2) is the number of bytes following byte 2 up to and including
        // the checksum: 2 header bytes (bytes 3-4) + 25 block bytes (bytes 5-29) + 1 checksum byte (byte 30) = 28 bytes.
        ext[2] = 28;
        ext[3] = 0x03; // PRODUCT_TYPE_MONITOR
        ext[4] = 0x00; // ext_count = 0
        ext[5] = 0x12; // DATA_BLOCK_TILED_DISPLAY
        ext[6] = 0x00; // block rev = 0
        ext[7] = 22; // payload length = 22 bytes
        ext[8] = (byte) 0x80; // single physical enclosure
        ext[9] = (byte) ((((numHTiles - 1) & 0x0F) << 4) | ((numVTiles - 1) & 0x0F));
        ext[10] = (byte) (((tileHLoc & 0x0F) << 4) | (tileVLoc & 0x0F));
        ext[11] = (byte) (((((numHTiles - 1) >> 4) & 0x03) << 6)
                | ((((numVTiles - 1) >> 4) & 0x03) << 4)
                | (((tileHLoc >> 4) & 0x03) << 2)
                | ((tileVLoc >> 4) & 0x03));
        ext[12] = (byte) ((tileWidth - 1) & 0xFF);
        ext[13] = (byte) (((tileWidth - 1) >> 8) & 0xFF);
        ext[14] = (byte) ((tileHeight - 1) & 0xFF);
        ext[15] = (byte) (((tileHeight - 1) >> 8) & 0xFF);
        byte[] topoBytes = topologyId.getBytes(StandardCharsets.US_ASCII);
        // In the 22-byte Tiled Topology Data Block, the Topology ID starts at offset 12 of the payload.
        // Since the payload starts at index 8, the ID starts at index 20.
        System.arraycopy(topoBytes, 0, ext, 20, Math.min(topoBytes.length, 9));
        int dispIdSum = 0;
        for (int i = 1; i <= 29; i++) {
            dispIdSum = (dispIdSum + (ext[i] & 0xFF)) & 0xFF;
        }
        ext[30] = (byte) ((256 - dispIdSum) & 0xFF);
        applyChecksum(ext, 0);
        return ext;
    }

    private void initVkmsWithDisconnectedExternalSlot() throws Exception {
        List<CfVkmsTester.VkmsConnectorSetup> config = new ArrayList<>();
        config.add(CfVkmsTester.VkmsConnectorSetup.builder()
                .setMonitor(CfVkmsTester.Monitor.REDRIX)
                .setEnabledAtStart(true)
                .build());
        config.add(CfVkmsTester.VkmsConnectorSetup.builder()
                .setMonitor(CfVkmsTester.Monitor.HP_SPECTRE32_4K_DP)
                .setEnabledAtStart(false)
                .build());
        mVkmsTester = CfVkmsTester.createWithConfig(getDevice(), config);
        assertNotNull("Failed to initialize VKMS tester for EDID Zoo", mVkmsTester);
        mVkmsTester.waitForDisplaysToBeOn(1, CfVkmsTester.DISPLAY_BRINGUP_TIMEOUT_MS);
    }

    private String getProcessPid(String processName) throws Exception {
        CommandResult res = getDevice().executeShellV2Command("pidof " + processName);
        return res.getStdout() == null ? "" : res.getStdout().trim();
    }

    private void waitForExternalDisplayDisconnected() throws Exception {
        for (int attempt = 0; attempt < 20; attempt++) {
            CommandResult res = getDevice().executeShellV2Command("dumpsys display");
            if (res.getStatus() == CommandStatus.SUCCESS && res.getStdout() != null) {
                boolean foundActiveExternal = false;
                for (String block : res.getStdout().split("(?=  Display Id=)")) {
                    if (block.contains("type EXTERNAL") && block.contains("mIsEnabled=true")) {
                        foundActiveExternal = true;
                        break;
                    }
                }
                if (!foundActiveExternal) {
                    return;
                }
            }
            RunUtil.getDefault().sleep(250);
        }
        fail("Timed out waiting for external display to disconnect");
    }

    private ExternalDisplayState waitForExternalLogicalDisplay(
            int expectedWidth, int expectedHeight) throws Exception {
        Pattern displayIdPattern = Pattern.compile("Display Id=(\\d+)");
        Pattern realSizePattern = Pattern.compile(
                "real (\\d+) x (\\d+),.*density (\\d+) \\([0-9.]+ x [0-9.]+\\) dpi");
        Pattern modePattern = Pattern.compile(
                "\\{id=(\\d+), width=(\\d+), height=(\\d+), fps=([0-9.]+)");

        ExternalDisplayState lastSeenState = null;
        for (int attempt = 0; attempt < 20; attempt++) {
            CommandResult res = getDevice().executeShellV2Command("dumpsys display");
            if (res.getStatus() == CommandStatus.SUCCESS && res.getStdout() != null) {
                String out = res.getStdout();
                String[] blocks = out.split("(?=  Display Id=)");
                for (String block : blocks) {
                    if (!block.contains("type EXTERNAL")
                            || !block.contains("mIsEnabled=true")) {
                        continue;
                    }
                    Matcher idMatcher = displayIdPattern.matcher(block);
                    Matcher sizeMatcher = realSizePattern.matcher(block);
                    if (idMatcher.find() && sizeMatcher.find()) {
                        ExternalDisplayState state = new ExternalDisplayState();
                        state.displayId = Integer.parseInt(idMatcher.group(1));
                        state.realWidth = Integer.parseInt(sizeMatcher.group(1));
                        state.realHeight = Integer.parseInt(sizeMatcher.group(2));
                        state.densityDpi = Integer.parseInt(sizeMatcher.group(3));

                        // Extract supportedModes specifically from the external DisplayDeviceInfo
                        for (String line : out.split("\n")) {
                            if (line.contains("DisplayDeviceInfo") && line.contains("EXTERNAL")
                                    && line.contains("supportedModes=[")) {
                                Matcher mMatcher = modePattern.matcher(line);
                                while (mMatcher.find()) {
                                    state.modes.add(mMatcher.group(2) + "x" + mMatcher.group(3)
                                            + "@" + mMatcher.group(4));
                                }
                                break;
                            }
                        }
                        if (state.modes.isEmpty()) {
                            Matcher mMatcher = modePattern.matcher(block);
                            while (mMatcher.find()) {
                                state.modes.add(mMatcher.group(2) + "x" + mMatcher.group(3) + "@"
                                        + mMatcher.group(4));
                            }
                        }
                        lastSeenState = state;
                        if (state.realWidth == expectedWidth
                                && state.realHeight == expectedHeight) {
                            return state;
                        }
                    }
                }
            }
            RunUtil.getDefault().sleep(500);
        }
        return lastSeenState;
    }

    private ExternalDisplayState swapEdidAndVerifyContract(String caseName, byte[] edidBytes,
            int expectedWidth, int expectedHeight, int expectedDensity) throws Exception {
        String sfPidBefore = getProcessPid("surfaceflinger");
        String hwcPidBefore = getProcessPid("android.hardware.graphics.composer3-service.drm");
        String sysServerPidBefore = getProcessPid("system_server");
        assertFalse("surfaceflinger PID must not be empty before " + caseName,
                sfPidBefore.isEmpty());
        assertFalse("hwc3 PID must not be empty before " + caseName,
                hwcPidBefore.isEmpty());
        assertFalse("system_server PID must not be empty before " + caseName,
                sysServerPidBefore.isEmpty());

        CommandResult unplugRes = getDevice().executeShellV2Command(
                "vkms_controller hotplug 1 disconnected");
        assertEquals("Failed to unplug connector 1 for " + caseName,
                CommandStatus.SUCCESS, unplugRes.getStatus());
        assertEquals("Non-zero exit code unplugging connector 1 for " + caseName,
                Integer.valueOf(0), unplugRes.getExitCode());
        waitForExternalDisplayDisconnected();

        String b64 = Base64.getEncoder().encodeToString(edidBytes);
        CommandResult writeRes = getDevice().executeShellV2Command(
                "echo '" + b64 + "' | base64 -d > " + VKMS_CONNECTOR_1_EDID_PATH);
        assertEquals("Failed to write raw EDID to ConfigFS for " + caseName,
                CommandStatus.SUCCESS, writeRes.getStatus());
        assertEquals("Non-zero exit code writing raw EDID to ConfigFS for " + caseName,
                Integer.valueOf(0), writeRes.getExitCode());

        CommandResult plugRes = getDevice().executeShellV2Command(
                "vkms_controller hotplug 1 connected");
        assertEquals("Failed to hotplug connector 1 for " + caseName,
                CommandStatus.SUCCESS, plugRes.getStatus());
        assertEquals("Non-zero exit code hotplugging connector 1 for " + caseName,
                Integer.valueOf(0), plugRes.getExitCode());

        ExternalDisplayState state = waitForExternalLogicalDisplay(expectedWidth, expectedHeight);
        assertNotNull("Timed out waiting for external LogicalDisplay on case " + caseName, state);

        // 1. Daemon stability check (no SurfaceFlinger, hwc3, or system_server crash/restart).
        assertEquals("surfaceflinger PID changed during " + caseName,
                sfPidBefore, getProcessPid("surfaceflinger"));
        assertEquals("hwc3 PID changed during " + caseName,
                hwcPidBefore, getProcessPid("android.hardware.graphics.composer3-service.drm"));
        assertEquals("system_server PID changed during " + caseName,
                sysServerPidBefore, getProcessPid("system_server"));

        // 2. Kernel DRM connector modes agreement & SurfaceFlinger commit health.
        // Query modes across any connected external (non-eDP) DRM connector (e.g. DP-1, Virtual-1, etc.).
        CommandResult kModesRes = getDevice().executeShellV2Command(
                "for d in /sys/class/drm/card*-*; do "
                        + "case \"$d\" in *eDP*) continue ;; esac; "
                        + "if [ -f \"$d/status\" ] && [ \"$(cat \"$d/status\" 2>/dev/null)\" = \"connected\" ] "
                        + "&& [ -f \"$d/modes\" ]; then cat \"$d/modes\"; fi; done");
        assertEquals("Kernel command to read external DRM modes failed for " + caseName,
                CommandStatus.SUCCESS, kModesRes.getStatus());
        assertEquals("Non-zero exit code on reading external DRM modes for " + caseName,
                Integer.valueOf(0), kModesRes.getExitCode());
        String kModes = kModesRes.getStdout();
        assertNotNull("No kernel DRM modes found for external connector on " + caseName, kModes);
        assertFalse("Kernel DRM modes file was empty or missing for external connector on " + caseName,
                kModes.trim().isEmpty());
        assertTrue("Kernel modes missing preferred " + expectedWidth + "x" + expectedHeight
                + " for " + caseName + ", found:\n" + kModes,
                kModes.contains(expectedWidth + "x" + expectedHeight));
        CommandResult sfDump = getDevice().executeShellV2Command("dumpsys SurfaceFlinger");
        if (sfDump.getStatus() == CommandStatus.SUCCESS && sfDump.getStdout() != null
                && sfDump.getStdout().contains("Failed to commit frames:")) {
            assertFalse("HWC failed to commit frames during " + caseName + ":\n" + sfDump.getStdout(),
                    Pattern.compile("Failed to commit frames: [1-9]").matcher(sfDump.getStdout()).find());
        }

        // 3. LogicalDisplay real resolution matches expected preferred mode.
        assertEquals("Unexpected LogicalDisplay real width for " + caseName,
                expectedWidth, state.realWidth);
        assertEquals("Unexpected LogicalDisplay real height for " + caseName,
                expectedHeight, state.realHeight);

        // 4. Density is clamped within [100, 400] and matches expected density when specified.
        assertTrue(String.format("Density %d outside [%d, %d] for %s",
                state.densityDpi, MIN_EXTERNAL_DENSITY_DPI, MAX_EXTERNAL_DENSITY_DPI, caseName),
                state.densityDpi >= MIN_EXTERNAL_DENSITY_DPI
                        && state.densityDpi <= MAX_EXTERNAL_DENSITY_DPI);
        if (expectedDensity > 0) {
            assertTrue(String.format("Expected density ~%d (+-2) for %s, got %d",
                    expectedDensity, caseName, state.densityDpi),
                    Math.abs(state.densityDpi - expectedDensity) <= 2);
        }

        // 5. Mode list sanity: non-empty, no 0 Hz modes, and no duplicate (W, H, fps) modes.
        assertFalse("Supported modes should not be empty for " + caseName, state.modes.isEmpty());
        Set<String> seenModes = new HashSet<>();
        for (String mode : state.modes) {
            String[] parts = mode.split("[@x]");
            int w = Integer.parseInt(parts[0]);
            int h = Integer.parseInt(parts[1]);
            float fps = Float.parseFloat(parts[2]);
            assertTrue("Invalid 0 Hz mode " + mode + " in " + caseName, fps > 0.0f);
            String normalized = w + "x" + h + "@" + Math.round(fps * 100.0f);
            assertTrue("Duplicate mode " + mode + " in " + caseName, seenModes.add(normalized));
        }

        // 6. Mode switch verification via cmd display set-user-preferred-display-mode.
        int modesTested = 0;
        try {
            for (String mode : state.modes) {
                if (modesTested >= 3) {
                    break;
                }
                String[] parts = mode.split("[@x]");
                CommandResult switchRes = getDevice().executeShellV2Command(String.format(
                        "cmd display set-user-preferred-display-mode %s %s %s %d",
                        parts[0], parts[1], parts[2], state.displayId));
                assertEquals("set-user-preferred-display-mode failed for " + mode + " in "
                        + caseName, CommandStatus.SUCCESS, switchRes.getStatus());
                assertEquals("Non-zero exit code on set-user-preferred-display-mode for " + mode
                        + " in " + caseName, Integer.valueOf(0), switchRes.getExitCode());
                modesTested++;
            }
        } finally {
            getDevice().executeShellV2Command(
                    "cmd display clear-user-preferred-display-mode " + state.displayId);
        }

        return state;
    }

    @Test
    public void testSize0_1080p() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd0mm = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 0, 0);
        byte[] edid = buildEdid(0xE001, 1001, "ZOO_SIZE0", 0, 0, 48, 75, 170,
                Arrays.asList(fhd0mm), null);
        swapEdidAndVerifyContract("size0_1080p", edid, 1920, 1080, 125);
    }

    @Test
    public void testAspectOnly_16_9() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd0mm = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 0, 0);
        byte[] edid = buildEdid(0xE002, 1002, "ZOO_ASPECT", 79, 0, 48, 75, 170,
                Arrays.asList(fhd0mm), null);
        swapEdidAndVerifyContract("aspect_only_16_9", edid, 1920, 1080, 125);
    }

    @Test
    public void testHeaderOnly_cm() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd0mm = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 0, 0);
        byte[] edid = buildEdid(0xE003, 1003, "ZOO_HDRCM", 53, 30, 48, 75, 170,
                Arrays.asList(fhd0mm), null);
        swapEdidAndVerifyContract("header_only_cm", edid, 1920, 1080, 125);
    }

    @Test
    public void testDtdOverridesMismatchHeader() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd24in = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        byte[] edid = buildEdid(0xE004, 1004, "ZOO_MISMATCH", 160, 90, 48, 75, 170,
                Arrays.asList(fhd24in), null);
        swapEdidAndVerifyContract("dtd_overrides_mismatch_header", edid, 1920, 1080, 126);
    }

    @Test
    public void testPortrait_1080x1920() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec portrait = new DtdSpec(148500, 1080, 280, 88, 44, 1920, 45, 4, 5, 298, 530);
        byte[] edid = buildEdid(0xE005, 1005, "ZOO_PORTRAIT", 30, 53, 48, 75, 170,
                Arrays.asList(portrait), null);
        swapEdidAndVerifyContract("portrait_1080x1920", edid, 1080, 1920, 126);
    }

    @Test
    public void testUltrawide_21_9() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec uw3440 = new DtdSpec(319750, 3440, 160, 48, 32, 1440, 41, 3, 10, 797, 334);
        DtdSpec uw2560 = new DtdSpec(181250, 2560, 160, 48, 32, 1080, 31, 3, 10, 797, 334);
        byte[] edid = buildEdid(0xE006, 1006, "ZOO_UW219", 80, 33, 48, 100, 350,
                Arrays.asList(uw3440, uw2560), null);
        swapEdidAndVerifyContract("ultrawide_21_9", edid, 3440, 1440, 150);
    }

    @Test
    public void testUltrawide_3840x1600() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec uw3840 = new DtdSpec(397000, 3840, 160, 48, 32, 1600, 56, 3, 10, 880, 367);
        byte[] edid = buildEdid(0xE007, 1007, "ZOO_UW3840", 88, 37, 48, 100, 420,
                Arrays.asList(uw3840), null);
        swapEdidAndVerifyContract("ultrawide_3840x1600", edid, 3840, 1600, 151);
    }

    @Test
    public void testFractional_5994_60_pair() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd6000 = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        DtdSpec fhd5994 = new DtdSpec(148350, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        byte[] edid = buildEdid(0xE008, 1008, "ZOO_5994_60", 53, 30, 48, 75, 170,
                Arrays.asList(fhd6000, fhd5994), null);
        swapEdidAndVerifyContract("fractional_5994_60_pair", edid, 1920, 1080, 126);
    }

    @Test
    public void testCinema_24hz_only() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd24 = new DtdSpec(74250, 1920, 830, 638, 44, 1080, 45, 4, 5, 530, 298);
        byte[] edid = buildEdid(0xE009, 1009, "ZOO_24HZ", 53, 30, 23, 30, 90,
                Arrays.asList(fhd24), null);
        swapEdidAndVerifyContract("cinema_24hz_only", edid, 1920, 1080, 126);
    }

    @Test
    public void testHigh_240hz() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd240 = new DtdSpec(594000, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        DtdSpec fhd60 = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        byte[] edid = buildEdid(0xE00A, 1010, "ZOO_240HZ", 53, 30, 48, 240, 620,
                Arrays.asList(fhd240, fhd60), null);
        swapEdidAndVerifyContract("high_240hz", edid, 1920, 1080, 126);
    }

    @Test
    public void testHuge_1600mm_4k() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec uhdHuge = new DtdSpec(594000, 3840, 560, 176, 88, 2160, 90, 8, 10, 1600, 900);
        byte[] edid = buildEdid(0xE00B, 1011, "ZOO_HUGE72", 160, 90, 48, 75, 620,
                Arrays.asList(uhdHuge), null);
        swapEdidAndVerifyContract("huge_1600mm_4k", edid, 3840, 2160, MIN_EXTERNAL_DENSITY_DPI);
    }

    @Test
    public void testTiny_10mm_4k() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec uhdTiny = new DtdSpec(594000, 3840, 560, 176, 88, 2160, 90, 8, 10, 10, 10);
        byte[] edid = buildEdid(0xE00C, 1012, "ZOO_TINY10", 1, 1, 48, 75, 620,
                Arrays.asList(uhdTiny), null);
        swapEdidAndVerifyContract("tiny_10mm_4k", edid, 3840, 2160, 251);
    }

    @Test
    public void testMany_modes_cta() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd60 = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        DtdSpec qhd60 = new DtdSpec(241500, 2560, 160, 48, 32, 1440, 41, 3, 5, 530, 298);
        DtdSpec uhd60 = new DtdSpec(594000, 3840, 560, 176, 88, 2160, 90, 8, 10, 530, 298);
        int[] vics = new int[] {
                1, 2, 3, 4, 16, 17, 18, 19, 31, 32, 33, 34, 60, 61, 62, 63,
                64, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106};
        byte[] cta = buildCtaExtension(vics, Arrays.asList(uhd60), false);
        byte[] edid = buildEdid(0xE00D, 1013, "ZOO_40MODES", 53, 30, 23, 120, 620,
                Arrays.asList(fhd60, qhd60), cta);
        ExternalDisplayState state = swapEdidAndVerifyContract(
                "many_modes_cta", edid, 1920, 1080, 126);
        assertTrue("Expected >= 15 modes for many_modes_cta, got " + state.modes.size(),
                state.modes.size() >= 15);
    }

    @Test
    public void testCta_hdr_audio() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec uhd60 = new DtdSpec(594000, 3840, 560, 176, 88, 2160, 90, 8, 10, 600, 340);
        int[] vics = new int[] {97, 96, 95, 93, 16, 31, 4};
        byte[] cta = buildCtaExtension(vics, null, true);
        byte[] edid = buildEdid(0xE00E, 1014, "ZOO_HDR_AUD", 60, 34, 23, 120, 620,
                Arrays.asList(uhd60), cta);
        swapEdidAndVerifyContract("cta_hdr_audio", edid, 3840, 2160, 222);
    }

    @Test
    public void testLiveConfigFsSwapUpdatesIdentityAndDensity() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec fhd0mm = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 0, 0);
        byte[] edid1 = buildEdid(0xE001, 1001, "ZOO_SIZE0", 0, 0, 48, 75, 170,
                Arrays.asList(fhd0mm), null);
        swapEdidAndVerifyContract("swap_step1_size0_1080p", edid1, 1920, 1080, 125);

        DtdSpec uw3440 = new DtdSpec(319750, 3440, 160, 48, 32, 1440, 41, 3, 10, 797, 334);
        byte[] edid2 = buildEdid(0xE006, 1006, "ZOO_UW219", 80, 33, 48, 100, 350,
                Arrays.asList(uw3440), null);
        swapEdidAndVerifyContract("swap_step2_ultrawide_21_9", edid2, 3440, 1440, 150);
    }

    @Test
    public void testDisplayId_tiledTopology() throws Exception {
        initVkmsWithDisconnectedExternalSlot();
        DtdSpec qhdTile0 = new DtdSpec(241500, 2560, 160, 48, 32, 1440, 41, 3, 5, 597, 336);
        byte[] displayIdTiled = buildDisplayIdTiledExtension(
                2, 1, 0, 0, 2560, 1440, "GGL5KTIL1");
        byte[] edid = buildEdid(0xE00F, 1015, "ZOO_TILED", 60, 34, 48, 75, 300,
                Arrays.asList(qhdTile0), displayIdTiled);
        swapEdidAndVerifyContract("displayid_tiled_topology", edid, 2560, 1440, 150);

        String modetestCmd = "modetest -M vkms -c";
        CommandResult whichRes = getDevice().executeShellV2Command("which modetest");
        if (whichRes.getStatus() != CommandStatus.SUCCESS || whichRes.getExitCode() != 0
                || whichRes.getStdout() == null || whichRes.getStdout().trim().isEmpty()) {
            CommandResult checkTmp = getDevice().executeShellV2Command("ls /data/local/tmp/modetest");
            if (checkTmp.getStatus() == CommandStatus.SUCCESS && checkTmp.getExitCode() == 0) {
                modetestCmd = "/data/local/tmp/modetest -M vkms -c";
            }
        }

        CommandResult modetestTiled = getDevice().executeShellV2Command(modetestCmd);
        assertEquals(modetestCmd + " failed for displayid_tiled_topology",
                CommandStatus.SUCCESS, modetestTiled.getStatus());
        assertEquals("Non-zero exit code on " + modetestCmd + " for displayid_tiled_topology",
                Integer.valueOf(0), modetestTiled.getExitCode());
        assertNotNull(modetestTiled.getStdout());
        assertTrue("Expected kernel DRM TILE blob to contain :1:2:1:0:0:2560:1440, got:\n"
                + modetestTiled.getStdout(),
                modetestTiled.getStdout().contains(":1:2:1:0:0:2560:1440"));

        DtdSpec fhd60 = new DtdSpec(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 530, 298);
        byte[] nonTiledEdid = buildEdid(0xE010, 1016, "ZOO_UNTILED", 53, 30, 48, 75, 170,
                Arrays.asList(fhd60), null);
        swapEdidAndVerifyContract("displayid_tiled_to_untiled", nonTiledEdid, 1920, 1080, 126);

        CommandResult modetestUntiled = getDevice().executeShellV2Command(modetestCmd);
        assertEquals(modetestCmd + " failed for displayid_tiled_to_untiled",
                CommandStatus.SUCCESS, modetestUntiled.getStatus());
        assertEquals("Non-zero exit code on " + modetestCmd + " for displayid_tiled_to_untiled",
                Integer.valueOf(0), modetestUntiled.getExitCode());
        assertNotNull(modetestUntiled.getStdout());
        assertFalse("Expected kernel DRM TILE blob to be cleared after swapping to untiled EDID",
                modetestUntiled.getStdout().contains(":1:2:1:0:0:2560:1440"));
    }
}
