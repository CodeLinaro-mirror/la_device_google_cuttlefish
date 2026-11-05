/*
 * Copyright 2021 The Android Open Source Project
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

#include <algorithm>
#include <cassert>
#include <optional>

#include <android-base/logging.h>
#include <keymaster/cppcose/cppcose.h>
#include <openssl/bn.h>
#include <openssl/bytestring.h>
#include <openssl/ec.h>
#include <openssl/err.h>
#include <openssl/hkdf.h>
#include <openssl/mldsa.h>
#include <openssl/rand.h>

#include "host/commands/secure_env/primary_key_builder.h"
#include "host/commands/secure_env/tpm_hmac.h"
#include "tpm_remote_provisioning_context.h"
#include "tpm_resource_manager.h"

using namespace cppcose;

namespace cuttlefish {

TpmRemoteProvisioningContext::TpmRemoteProvisioningContext(
    TpmResourceManager& resource_manager)
    : resource_manager_(resource_manager) {
  std::tie(devicePrivKey_, bcc_) = GenerateBcc(/*testMode=*/false);
}

std::vector<uint8_t> TpmRemoteProvisioningContext::DeriveBytesFromHbk(
    const std::string& context, size_t num_bytes) const {
  std::vector<uint8_t> result(num_bytes);
  auto hbk = TpmHmacWithContext(
      resource_manager_, "HardwareBoundKey",
      reinterpret_cast<const uint8_t*>(context.data()), context.size());
  if (!hbk) {
    LOG(ERROR) << "Error calculating HMAC";
    return result;
  }

  if (!HKDF(result.data(), num_bytes,              //
            EVP_sha256(),                          //
            hbk->buffer, hbk->size,                //
            nullptr /* salt */, 0 /* salt len */,  //
            reinterpret_cast<const uint8_t*>(context.data()), context.size())) {
    // Should never fail. Even if it could the API has no way of reporting the
    // error.
    LOG(ERROR) << "Error calculating HKDF: " << ERR_peek_last_error();
  }

  return result;
}

std::unique_ptr<cppbor::Map> TpmRemoteProvisioningContext::CreateDeviceInfo(
    uint32_t csrVersion) const {
  auto result = std::make_unique<cppbor::Map>();
  result->add(cppbor::Tstr("brand"), cppbor::Tstr("Google"));
  result->add(cppbor::Tstr("manufacturer"), cppbor::Tstr("Google"));
  result->add(cppbor::Tstr("product"),
              cppbor::Tstr("Cuttlefish Virtual Device"));
  result->add(cppbor::Tstr("model"), cppbor::Tstr("Virtual Device"));
  result->add(cppbor::Tstr("device"), cppbor::Tstr("Virtual Device"));
  if (bootloader_state_) {
    result->add(cppbor::Tstr("bootloader_state"),
                cppbor::Tstr(*bootloader_state_));
  }
  if (verified_boot_state_) {
    result->add(cppbor::Tstr("vb_state"), cppbor::Tstr(*verified_boot_state_));
  }
  if (vbmeta_digest_) {
    result->add(cppbor::Tstr("vbmeta_digest"), cppbor::Bstr(*vbmeta_digest_));
  }
  if (os_version_) {
    result->add(cppbor::Tstr("os_version"),
                cppbor::Tstr(std::to_string(*os_version_)));
  }
  if (os_patchlevel_) {
    result->add(cppbor::Tstr("system_patch_level"),
                cppbor::Uint(*os_patchlevel_));
  }
  if (boot_patchlevel_) {
    result->add(cppbor::Tstr("boot_patch_level"),
                cppbor::Uint(*boot_patchlevel_));
  }
  if (vendor_patchlevel_) {
    result->add(cppbor::Tstr("vendor_patch_level"),
                cppbor::Uint(*vendor_patchlevel_));
  }
  // "version" field was removed from DeviceInfo in CSR v3.
  if (csrVersion < 3) {
    result->add(cppbor::Tstr("version"), cppbor::Uint(csrVersion));
  }
  result->add(cppbor::Tstr("fused"), cppbor::Uint(0));
  result->add(cppbor::Tstr("security_level"), cppbor::Tstr("tee"));
  result->canonicalize();
  return result;
}

std::pair<std::vector<uint8_t> /* privKey */, cppbor::Array /* BCC */>
TpmRemoteProvisioningContext::GenerateBcc(bool testMode) const {
  std::vector<uint8_t> uds_seed;
  std::vector<uint8_t> stage1_seed;
  std::vector<uint8_t> stage2_seed;

#ifdef DICE_PROFILE_ANDROID_18
  constexpr size_t kUdsSeedBytes = MLDSA_SEED_BYTES;
  constexpr std::string_view kProfileName = "android.18";
#else
  constexpr size_t kUdsSeedBytes = 32;
  constexpr std::string_view kProfileName = "android.16";
#endif

  if (testMode) {
    uds_seed.resize(kUdsSeedBytes);
    RAND_bytes(uds_seed.data(), uds_seed.size());
    stage1_seed.resize(32);
    RAND_bytes(stage1_seed.data(), stage1_seed.size());
    stage2_seed.resize(32);
    RAND_bytes(stage2_seed.data(), stage2_seed.size());
  } else {
    uds_seed = DeriveBytesFromHbk("UdsKey", kUdsSeedBytes);
    stage1_seed = DeriveBytesFromHbk("Stage1Key", 32);
    stage2_seed = DeriveBytesFromHbk("Stage2Key", 32);
  }

#ifdef DICE_PROFILE_ANDROID_18
  auto uds_priv = std::make_unique<MLDSA87_private_key>();
  if (!MLDSA87_private_key_from_seed(uds_priv.get(), uds_seed.data(),
                                     uds_seed.size())) {
    LOG(FATAL) << "Failed to derive ML-DSA-87 private key from seed";
  }
  auto uds_pub_struct = std::make_unique<MLDSA87_public_key>();
  if (!MLDSA87_public_from_private(uds_pub_struct.get(), uds_priv.get())) {
    LOG(FATAL) << "Failed to extract ML-DSA-87 public key";
  }
  std::vector<uint8_t> uds_pub(MLDSA87_PUBLIC_KEY_BYTES);
  CBB cbb;
  CBB_init_fixed(&cbb, uds_pub.data(), uds_pub.size());
  if (!MLDSA87_marshal_public_key(&cbb, uds_pub_struct.get())) {
    LOG(FATAL) << "Failed to marshal ML-DSA-87 public key";
  }

  auto udsCoseKey = cppbor::Map()
                        .add(CoseKey::KEY_TYPE, ALGORITHM_KEY_PAIR)
                        .add(CoseKey::ALGORITHM, ML_DSA_87)
                        .add(CoseKey::KEY_OPS, cppbor::Array().add(VERIFY))
                        .add(CoseKey::AKP_PUBLIC_KEY, uds_pub)
                        .canonicalize();
#else
  std::vector<uint8_t> uds_priv(ED25519_PRIVATE_KEY_LEN);
  std::vector<uint8_t> uds_pub(ED25519_PUBLIC_KEY_LEN);
  ED25519_keypair_from_seed(uds_pub.data(), uds_priv.data(), uds_seed.data());

  auto udsCoseKey = cppbor::Map()
                        .add(CoseKey::KEY_TYPE, OCTET_KEY_PAIR)
                        .add(CoseKey::ALGORITHM, EDDSA)
                        .add(CoseKey::CURVE, ED25519)
                        .add(CoseKey::PUBKEY_X, uds_pub)
                        .canonicalize();
#endif

  std::vector<uint8_t> stage1_priv(ED25519_PRIVATE_KEY_LEN);
  std::vector<uint8_t> stage1_pub(ED25519_PUBLIC_KEY_LEN);
  ED25519_keypair_from_seed(stage1_pub.data(), stage1_priv.data(),
                            stage1_seed.data());

  std::vector<uint8_t> stage2_priv(ED25519_PRIVATE_KEY_LEN);
  std::vector<uint8_t> stage2_pub(ED25519_PUBLIC_KEY_LEN);
  ED25519_keypair_from_seed(stage2_pub.data(), stage2_priv.data(),
                            stage2_seed.data());

  auto stage1CoseKey = cppbor::Map()
                           .add(CoseKey::KEY_TYPE, OCTET_KEY_PAIR)
                           .add(CoseKey::ALGORITHM, EDDSA)
                           .add(CoseKey::CURVE, ED25519)
                           .add(CoseKey::PUBKEY_X, stage1_pub)
                           .canonicalize();
  auto configDescStage1 = cppbor::Map()
                              .add(-70002 /* Component Name */, "Stage 1")
                              .add(-70005 /* Security Version */, 1)
                              .canonicalize()
                              .encode();
  auto cert1Payload =
      cppbor::Map()
          .add(1 /* Issuer */, "UDS")
          .add(2 /* Subject */, "Stage 1")
          .add(-4670552 /* Subject Pub Key */, stage1CoseKey.encode())
          .add(-4670553 /* Key Usage (little-endian order) */,
               std::vector<uint8_t>{0x20} /* keyCertSign = 1<<5 */)
          .add(-4670551 /* Mode */, std::vector<uint8_t>{1} /* Normal */)
          .add(-4670545 /* Code Hash */, std::vector<uint8_t>(64, 0))
          .add(-4670548 /* Config Desc */, configDescStage1)
          .add(-4670549 /* Authority Hash */, std::vector<uint8_t>(64, 0))
          .add(-4670554 /* Profile Name */, std::string(kProfileName))
          .canonicalize()
          .encode();
#ifdef DICE_PROFILE_ANDROID_18
  auto cert1 = constructCoseSign1(uds_seed,      /* signing key seed */
                                  cppbor::Map(), /* extra protected */
                                  cert1Payload, {} /* AAD */, ML_DSA_87);
#else
  auto cert1 = constructEdDsaCoseSign1(uds_priv,      /* signing key */
                                       cppbor::Map(), /* extra protected */
                                       cert1Payload, {} /* AAD */);
#endif
  assert(cert1);

  auto stage2CoseKey = cppbor::Map()
                           .add(CoseKey::KEY_TYPE, OCTET_KEY_PAIR)
                           .add(CoseKey::ALGORITHM, EDDSA)
                           .add(CoseKey::CURVE, ED25519)
                           .add(CoseKey::PUBKEY_X, stage2_pub)
                           .canonicalize();
  auto configDescStage2 =
      cppbor::Map()
          .add(-70002 /* Component Name */, "Stage 2 (KeyMint)")
          .add(-70005 /* Security Version */, 1)
          .canonicalize()
          .encode();
  auto cert2Payload =
      cppbor::Map()
          .add(1 /* Issuer */, "Stage 1")
          .add(2 /* Subject */, "Stage 2 (KeyMint)")
          .add(-4670552 /* Subject Pub Key */, stage2CoseKey.encode())
          .add(-4670553 /* Key Usage (little-endian order) */,
               std::vector<uint8_t>{0x20} /* keyCertSign = 1<<5 */)
          .add(-4670551 /* Mode */, std::vector<uint8_t>{1} /* Normal */)
          .add(-4670545 /* Code Hash */, std::vector<uint8_t>(64, 0))
          .add(-4670548 /* Config Desc */, configDescStage2)
          .add(-4670549 /* Authority Hash */, std::vector<uint8_t>(64, 0))
          .add(-4670554 /* Profile Name */, std::string(kProfileName))
          .canonicalize()
          .encode();
  auto cert2 = constructEdDsaCoseSign1(stage1_priv,   /* signing key */
                                       cppbor::Map(), /* extra protected */
                                       cert2Payload, {} /* AAD */);
  assert(cert2);

  cppbor::Array bcc;
  bcc.add(std::move(udsCoseKey));
  bcc.add(cert1.moveValue());
  bcc.add(cert2.moveValue());

  return {stage2_priv, std::move(bcc)};
}

void TpmRemoteProvisioningContext::SetSystemVersion(uint32_t os_version,
                                                    uint32_t os_patchlevel) {
  os_version_ = os_version;
  os_patchlevel_ = os_patchlevel;
}

void TpmRemoteProvisioningContext::SetVendorPatchlevel(
    uint32_t vendor_patchlevel) {
  vendor_patchlevel_ = vendor_patchlevel;
}

void TpmRemoteProvisioningContext::SetBootPatchlevel(uint32_t boot_patchlevel) {
  boot_patchlevel_ = boot_patchlevel;
}

void TpmRemoteProvisioningContext::SetVerifiedBootInfo(
    std::string_view boot_state, std::string_view bootloader_state,
    const std::vector<uint8_t>& vbmeta_digest) {
  verified_boot_state_ = boot_state;
  bootloader_state_ = bootloader_state;
  vbmeta_digest_ = vbmeta_digest;
}

ErrMsgOr<std::vector<uint8_t>>
TpmRemoteProvisioningContext::BuildProtectedDataPayload(
    bool isTestMode,                     //
    const std::vector<uint8_t>& macKey,  //
    const std::vector<uint8_t>& aad) const {
  std::vector<uint8_t> devicePrivKey;
  cppbor::Array bcc;
  if (isTestMode) {
    std::tie(devicePrivKey, bcc) = GenerateBcc(/*testMode=*/true);
  } else {
    devicePrivKey = devicePrivKey_;
    auto clone = bcc_.clone();
    if (!clone->asArray()) {
      return "The BCC is not an array";
    }
    bcc = std::move(*clone->asArray());
  }
  auto sign1 = constructEdDsaCoseSign1(devicePrivKey, {} /* extra protected */,
                                       macKey, aad);
  if (!sign1) {
    return sign1.moveMessage();
  }
  return cppbor::Array().add(sign1.moveValue()).add(std::move(bcc)).encode();
}

std::optional<cppcose::HmacSha256>
TpmRemoteProvisioningContext::GenerateHmacSha256(
    const cppcose::bytevec& input) const {
  auto tpm_digest =
      TpmHmacWithContext(resource_manager_, "Public Key Authentication Key",
                         input.data(), input.size());
  if (!tpm_digest) {
    LOG(ERROR) << "Could not calculate hmac";
    return std::nullopt;
  }

  cppcose::HmacSha256 hmac;
  if (tpm_digest->size != hmac.size()) {
    LOG(ERROR) << "TPM-generated digest was too short. Actual size: "
               << tpm_digest->size << " expected " << hmac.size() << " bytes";
    return std::nullopt;
  }

  std::copy(tpm_digest->buffer, tpm_digest->buffer + tpm_digest->size,
            hmac.begin());
  return hmac;
}

void TpmRemoteProvisioningContext::GetHwInfo(
    keymaster::GetHwInfoResponse* hwInfo) const {
  hwInfo->version = 3;
  hwInfo->rpcAuthorName = "Google";
  hwInfo->supportedEekCurve = 0 /* CURVE_NONE */;
  hwInfo->uniqueId = "remote keymint";
  hwInfo->supportedNumKeysInCsr = 20;
}

cppcose::ErrMsgOr<cppbor::Array> TpmRemoteProvisioningContext::BuildCsr(
    const std::vector<uint8_t>& challenge, cppbor::Array keysToSign) const {
  uint32_t csrVersion = 3;
  auto deviceInfo = std::move(*CreateDeviceInfo(csrVersion));
  auto csrPayload = cppbor::Array()
                        .add(csrVersion)
                        .add("keymint" /* CertificateType */)
                        .add(std::move(deviceInfo))
                        .add(std::move(keysToSign));
  auto signedDataPayload =
      cppbor::Array().add(challenge).add(cppbor::Bstr(csrPayload.encode()));
  auto signedData =
      constructEdDsaCoseSign1(devicePrivKey_, {} /* extra protected */,
                              signedDataPayload.encode(), {} /* aad */);

  return cppbor::Array()
      .add(1 /* version */)
      .add(cppbor::Map() /* UdsCerts */)
      .add(std::move(*bcc_.clone()->asArray()) /* DiceCertChain */)
      .add(std::move(*signedData) /* SignedData */);
}

}  // namespace cuttlefish
