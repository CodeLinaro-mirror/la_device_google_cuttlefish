/*
 * Copyright (C) 2024 The Android Open Source Project
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

#include "SecureElement.h"

#include <format>
#include <string>

#define LOG_TAG "jcardsim"

#include <android-base/logging.h>

namespace aidl::android::hardware::secure_element {
constexpr const int kUnusedCommandField = 0;
constexpr int32_t kSuccess = 0x9000;
constexpr int32_t kMaxAidLen = 16;
constexpr uint8_t kSelectIns = 0xA4;
constexpr uint8_t kP1SelectByAid = 0x04;
constexpr uint8_t kP1ManageChannelClose = 0x80;
constexpr uint8_t kManageChannelIns = 0x70;

namespace {
using cuttlefish::ErrorFromType;
using cuttlefish::OutcomeDereference;
using cuttlefish::StackTraceEntry;
using cuttlefish::TypeIsSuccess;

Result<void> ResponseOK(const std::vector<uint8_t>& response) {
    CF_EXPECT(response.size() >= 2, "Response Size less than 2");
    size_t size = response.size();
    CF_EXPECT(((response[size - 2] << 8) | response[size - 1]) == kSuccess,
              "Status Code: " << (response[size - 2] << 8 | response[size - 1]));
    return {};
}

std::string toHexString(const std::vector<uint8_t>& data) {
    std::string hexStr;
    hexStr.reserve(data.size() * 2);

    for (auto ch : data) {
        hexStr += std::format("{:02X}", ch);
    }
    return hexStr;
}

}  // namespace

SecureElement::SecureElement(std::shared_ptr<SharedFdChannel> channel) : channel_(channel) {}

Result<ManagedMessage> SecureElement::toMessage(const std::vector<uint8_t>& data) {
    auto msg = CF_EXPECT(cuttlefish::transport::CreateMessage(kUnusedCommandField, data.size()));
    std::copy(data.begin(), data.end(), msg->payload);
    return msg;
}

Result<std::vector<uint8_t>> SecureElement::fromMessage(ManagedMessage& message) {
    std::vector<uint8_t> res;
    const uint8_t* buffer = message->payload;
    const uint8_t* buffer_end = message->payload + message->payload_size;
    if (message->payload_size > 0) {
        res.insert(res.begin(), buffer, buffer_end);
    }
    return res;
}

Result<void> SecureElement::forwardCommand(const std::vector<uint8_t>& req,
                                           std::vector<uint8_t>& res) {
    auto msg = CF_EXPECT(toMessage(req), "Failed to create message from the request");
    LOG(DEBUG) << "Request:" << toHexString(req);
    CF_EXPECT(channel_->SendRequest(*msg), "Failed to send request");
    CF_EXPECT(channel_->WaitForMessage(), "Failed to wait for command response");
    auto response = CF_EXPECT(channel_->ReceiveMessage(), "Failed to receive response");
    auto result = CF_EXPECT(fromMessage(response), "Failed to read from Message");
    res = std::move(result);
    LOG(DEBUG) << "Response:" << toHexString(res);
    return {};
}

ScopedAStatus SecureElement::init(const std::shared_ptr<ISecureElementCallback>& client_callback) {
    if (client_callback == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_NULL_POINTER);
    }
    callback_ = client_callback;
    callback_->onStateChange(true, "init");
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::getAtr(std::vector<uint8_t>* aidl_return) {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    std::vector<uint8_t> const atr{};
    *aidl_return = atr;
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::isCardPresent(bool* aidl_return) {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    *aidl_return = true;
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::reset() {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    callback_->onStateChange(false, "reset");
    callback_->onStateChange(true, "reset");
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::resetWithReason(ResetReason reason) {
    // The reason is advisory. jcardsim performs the same reset for every reason,
    // including unrecognized values, which are treated as ResetReason::UNKNOWN.
    LOG(INFO) << __func__ << " reason: " << toString(reason);
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    callback_->onStateChange(false, "resetWithReason");
    callback_->onStateChange(true, "resetWithReason");
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::transmit(const std::vector<uint8_t>& data,
                                      std::vector<uint8_t>* aidl_return) {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    std::vector<uint8_t> output;
    if (!forwardCommand(data, output).has_value()) {
        LOG(ERROR) << "Failed to transmit.";
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }
    *aidl_return = output;
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::openLogicalChannel(
    const std::vector<uint8_t>& aid, int8_t p2,
    ::aidl::android::hardware::secure_element::LogicalChannelResponse* aidl_return) {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }

    // Execute MANAGE CHANNEL. According to GlobalPlatform Card Specification, Section:11.7.3,
    // the assigned channel number is returned upon success.
    auto manageChannelRes = executeManageChannel(0 /* CLA */, 0 /* p1 */, 0 /* p2 */, 1 /* le */);
    if (!manageChannelRes.has_value()) {
        LOG(ERROR) << "Failed in ManageChannelCommand - " << manageChannelRes.error().Message();
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }

    uint8_t cla;
    uint8_t channelNumber = (*manageChannelRes)[0];
    if ((channelNumber > 0x03) && (channelNumber < 0x14)) {
        /* update CLA byte according to GP spec Table 11-12*/
        cla = 0x40 + (channelNumber - 4); /* Class of instruction */
    } else if ((channelNumber > 0x00) && (channelNumber < 0x04)) {
        /* update CLA byte according to GP spec Table 11-11*/
        cla = channelNumber; /* Class of instruction */
    } else {
        LOG(ERROR) << "Invalid Channel " << channelNumber;
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }

    auto selectResponse = executeSelect(cla, p2, aid);
    if (!selectResponse.has_value()) {
        LOG(ERROR) << "Failed to open logical channel - " << selectResponse.error().Message();
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }

    aidl_return->channelNumber = channelNumber;
    aidl_return->selectResponse = std::move(*selectResponse);
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::openBasicChannel(const std::vector<uint8_t>& aid, int8_t p2,
                                              std::vector<uint8_t>* aidl_return) {

    auto selectResponse = executeSelect(0 /* CLA */, p2, aid);
    if (!selectResponse.has_value()) {
        LOG(ERROR) << "Failed to open basic channel - " << selectResponse.error().Message();
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }

    *aidl_return = std::move(*selectResponse);
    return ScopedAStatus::ok();
}

ScopedAStatus SecureElement::closeChannel(int8_t channelNumber) {
    if (callback_ == nullptr) {
        return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }

    uint8_t cla = channelNumber;
    // For Supplementary Channel update CLA byte according to GP
    if ((channelNumber > 0x03) && (channelNumber < 0x14)) {
        /* update CLA byte according to GP spec Table 11-12*/
        cla = 0x40 + (channelNumber - 4);
    }

    auto result =
        executeManageChannel(cla, kP1ManageChannelClose, channelNumber /* p2 */, 0 /* le */);
    if (!result.has_value()) {
        LOG(ERROR) << "closeChannel failed - " << result.error().Message();
        return ScopedAStatus::fromServiceSpecificError(IOERROR);
    }

    return ScopedAStatus::ok();
}

Result<std::vector<uint8_t>> SecureElement::executeSelect(uint8_t cla, uint8_t p2,
                                                          const std::vector<uint8_t>& aid) {
    size_t aidLen = aid.size();
    CF_EXPECT(aidLen <= kMaxAidLen,
              "AID length " << aidLen << " exceeds maximum allowed length of " << kMaxAidLen);

    // Command APDU encoding options:
    //
    // case 2s: |CLA|INS|P1 |P2 |LE |
    // case 4s: |CLA|INS|P1 |P2 |LC |...BODY...|LE |
    std::vector<uint8_t> selectCmd;
    size_t cmdLengthValue = aid.empty() ? 0 : (1 + aidLen);
    selectCmd.reserve(5 + cmdLengthValue);

    selectCmd.push_back(cla);        /* CLA */
    selectCmd.push_back(kSelectIns); /* Instruction code */
    selectCmd.push_back(
        kP1SelectByAid);     /* Instruction parameter 1 (Select by Dedicated File (DF) Name) */
    selectCmd.push_back(p2); /* Instruction parameter 2 */
    if (aidLen != 0) {
        selectCmd.push_back(aidLen);
        selectCmd.insert(selectCmd.end(), aid.begin(), aid.end());
    }
    selectCmd.push_back(0x00);

    std::vector<uint8_t> resApduBuff;
    CF_EXPECT(forwardCommand(selectCmd, resApduBuff), "select cmd failed.");
    CF_EXPECT(ResponseOK(resApduBuff));

    return resApduBuff;
}

Result<std::vector<uint8_t>> SecureElement::executeManageChannel(uint8_t cla, uint8_t p1,
                                                                 uint8_t p2, uint8_t le) {
    std::vector<uint8_t> manageChannelCommand = {cla, kManageChannelIns, p1, p2, le};

    std::vector<uint8_t> resApduBuff;
    CF_EXPECT(forwardCommand(manageChannelCommand, resApduBuff), "manage channel cmd failed.");
    CF_EXPECT(ResponseOK(resApduBuff));

    return resApduBuff;
}

}  // namespace aidl::android::hardware::secure_element
