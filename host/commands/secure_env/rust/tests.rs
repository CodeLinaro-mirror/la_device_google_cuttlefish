//! Tests for Cuttlefish-specific code.
use crate::soft;

/// CBOR map key for `profile_name` in `DiceChainEntryPayload` (see generateCertificateRequestV2.cddl).
const PROFILE_NAME: i64 = -4670554;

#[test]
fn test_signing_cert_parse() {
    let sign_info = kmr_ta_nonsecure::attest::CertSignInfo::new();
    kmr_tests::test_signing_cert_parse(sign_info, false);
}

#[test]
fn test_rkp_soft_trait() {
    let hmac = kmr_crypto_boring::hmac::BoringHmac;
    let soft_rpc = soft::RpcArtifacts::new(
        soft::Derive::default(),
        kmr_ta::device::CsrSigningAlgorithm::EdDSA,
    );
    kmr_tests::test_retrieve_rpc_artifacts(soft_rpc, &hmac, &hmac);
}

#[test]
fn test_rkp_soft_trait_es256() {
    let hmac = kmr_crypto_boring::hmac::BoringHmac;
    let soft_rpc = soft::RpcArtifacts::new(
        soft::Derive::default(),
        kmr_ta::device::CsrSigningAlgorithm::ES256,
    );
    kmr_tests::test_retrieve_rpc_artifacts(soft_rpc, &hmac, &hmac);
}

#[test]
fn test_rkp_soft_trait_es384() {
    let hmac = kmr_crypto_boring::hmac::BoringHmac;
    let soft_rpc = soft::RpcArtifacts::new(
        soft::Derive::default(),
        kmr_ta::device::CsrSigningAlgorithm::ES384,
    );
    kmr_tests::test_retrieve_rpc_artifacts(soft_rpc, &hmac, &hmac);
}

#[test]
fn test_rkp_mldsa_uds() {
    use kmr_ta::device::RetrieveRpcArtifacts;
    use kmr_wire::cbor::value::Value;
    use kmr_wire::coset::{
        AsCborValue, CoseKey, CoseSign1, Label, RegisteredLabel, RegisteredLabelWithPrivate, iana,
    };
    use kmr_wire::read_to_value;

    let hmac = kmr_crypto_boring::hmac::BoringHmac;
    let soft_rpc = soft::RpcArtifacts::new_with_profile(
        Some(String::from(kmr_ta_nonsecure::rpc::ANDROID_18_PROFILE_NAME)),
        soft::Derive::default(),
        kmr_ta::device::CsrSigningAlgorithm::EdDSA,
    );
    let dice_info =
        soft_rpc.get_dice_info(kmr_wire::rpc::TestMode(false)).expect("Failed to get DICE info");

    // 1. Deserialize DiceCertChain CBOR array: [UDS_Pub, cert1, cert2]
    let chain_val = read_to_value(&dice_info.pub_dice_artifacts.dice_cert_chain)
        .expect("Failed to decode DICE cert chain");
    let chain = match chain_val {
        Value::Array(arr) => arr,
        _ => panic!("Expected CBOR array for DiceCertChain"),
    };
    assert_eq!(chain.len(), 3, "Expected 3 entries in DICE chain");

    // 2. Verify chain[0] (UDS_Pub) is an ML-DSA-87 AKP CoseKey
    let uds_cose_key = CoseKey::from_cbor_value(chain[0].clone()).expect("Invalid UDS CoseKey");
    assert_eq!(uds_cose_key.kty, RegisteredLabel::Assigned(iana::KeyType::AKP));
    assert_eq!(
        uds_cose_key.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::ML_DSA_87))
    );
    assert!(
        !uds_cose_key
            .params
            .iter()
            .any(|(l, _)| *l == Label::Int(kmr_common::crypto::ec::RKP_TEST_KEY_CBOR_MARKER)),
        "Unexpected RKP test key marker in UDS CoseKey"
    );
    let pub_param = uds_cose_key
        .params
        .iter()
        .find(|(l, _)| *l == Label::Int(iana::AkpKeyParameter::Pub as i64))
        .expect("Missing pub parameter (-1) in AKP CoseKey");
    match &pub_param.1 {
        Value::Bytes(b) => assert_eq!(b.len(), 2592, "ML-DSA-87 public key must be 2592 bytes"),
        _ => panic!("Expected bstr for ML-DSA-87 public key"),
    }

    // 3. Verify chain[1] (cert1: UDS -> Stage 1) is signed with ML-DSA-87 and has profile "android.18"
    let cert1 = CoseSign1::from_cbor_value(chain[1].clone()).expect("Invalid cert1 CoseSign1");
    assert_eq!(
        cert1.protected.header.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::ML_DSA_87))
    );
    assert_eq!(cert1.signature.len(), 4627, "ML-DSA-87 signature must be 4627 bytes");
    let cert1_payload =
        read_to_value(cert1.payload.as_ref().unwrap()).expect("Invalid cert1 payload");
    if let Value::Map(entries) = cert1_payload {
        let profile = entries
            .iter()
            .find(|(k, _)| *k == Value::Integer(PROFILE_NAME.into()))
            .expect("Missing profile_name in cert1");
        assert_eq!(profile.1, Value::Text(String::from("android.18")));
    } else {
        panic!("Expected Map for cert1 payload");
    }

    // 4. Verify chain[2] (cert2: Stage 1 -> Stage 2) is signed with EdDSA and has profile "android.18"
    let cert2 = CoseSign1::from_cbor_value(chain[2].clone()).expect("Invalid cert2 CoseSign1");
    assert_eq!(
        cert2.protected.header.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::EdDSA))
    );
    assert_eq!(cert2.signature.len(), 64, "Ed25519 signature must be 64 bytes");
    let cert2_payload =
        read_to_value(cert2.payload.as_ref().unwrap()).expect("Invalid cert2 payload");
    if let Value::Map(entries) = cert2_payload {
        let profile = entries
            .iter()
            .find(|(k, _)| *k == Value::Integer(PROFILE_NAME.into()))
            .expect("Missing profile_name in cert2");
        assert_eq!(profile.1, Value::Text(String::from("android.18")));
    } else {
        panic!("Expected Map for cert2 payload");
    }

    kmr_tests::test_retrieve_rpc_artifacts(soft_rpc, &hmac, &hmac);
}

#[test]
fn test_rkp_legacy_profile_uds() {
    use kmr_ta::device::RetrieveRpcArtifacts;
    use kmr_wire::cbor::value::Value;
    use kmr_wire::coset::{
        AsCborValue, CoseKey, CoseSign1, RegisteredLabel, RegisteredLabelWithPrivate, iana,
    };
    use kmr_wire::read_to_value;

    let hmac = kmr_crypto_boring::hmac::BoringHmac;
    let soft_rpc = soft::RpcArtifacts::new_with_profile(
        Some(String::from("android.15")),
        soft::Derive::default(),
        kmr_ta::device::CsrSigningAlgorithm::EdDSA,
    );
    let dice_info =
        soft_rpc.get_dice_info(kmr_wire::rpc::TestMode(false)).expect("Failed to get DICE info");

    // Deserialize DiceCertChain CBOR array: [UDS_Pub, cert1, cert2]
    let chain_val = read_to_value(&dice_info.pub_dice_artifacts.dice_cert_chain)
        .expect("Failed to decode DICE cert chain");
    let chain = match chain_val {
        Value::Array(arr) => arr,
        _ => panic!("Expected CBOR array for DiceCertChain"),
    };
    assert_eq!(chain.len(), 3, "Expected 3 entries in DICE chain");

    // Verify chain[0] (UDS_Pub) is an OKP EdDSA CoseKey for pre-android.18 profiles
    let uds_cose_key = CoseKey::from_cbor_value(chain[0].clone()).expect("Invalid UDS CoseKey");
    assert_eq!(uds_cose_key.kty, RegisteredLabel::Assigned(iana::KeyType::OKP));
    assert_eq!(
        uds_cose_key.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::EdDSA))
    );

    // Verify chain[1] (cert1) is signed with EdDSA and has profile "android.15"
    let cert1 = CoseSign1::from_cbor_value(chain[1].clone()).expect("Invalid cert1 CoseSign1");
    assert_eq!(
        cert1.protected.header.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::EdDSA))
    );
    assert_eq!(cert1.signature.len(), 64, "Ed25519 signature must be 64 bytes");
    let cert1_payload =
        read_to_value(cert1.payload.as_ref().unwrap()).expect("Invalid cert1 payload");
    if let Value::Map(entries) = cert1_payload {
        let profile = entries
            .iter()
            .find(|(k, _)| *k == Value::Integer(PROFILE_NAME.into()))
            .expect("Missing profile_name in cert1");
        assert_eq!(profile.1, Value::Text(String::from("android.15")));
    } else {
        panic!("Expected Map for cert1 payload");
    }

    // Verify chain[2] (cert2: Stage 1 -> Stage 2) is signed with EdDSA and has profile "android.15"
    let cert2 = CoseSign1::from_cbor_value(chain[2].clone()).expect("Invalid cert2 CoseSign1");
    assert_eq!(
        cert2.protected.header.alg,
        Some(RegisteredLabelWithPrivate::Assigned(iana::Algorithm::EdDSA))
    );
    assert_eq!(cert2.signature.len(), 64, "Ed25519 signature must be 64 bytes");
    let cert2_payload =
        read_to_value(cert2.payload.as_ref().unwrap()).expect("Invalid cert2 payload");
    if let Value::Map(entries) = cert2_payload {
        let profile = entries
            .iter()
            .find(|(k, _)| *k == Value::Integer(PROFILE_NAME.into()))
            .expect("Missing profile_name in cert2");
        assert_eq!(profile.1, Value::Text(String::from("android.15")));
    } else {
        panic!("Expected Map for cert2 payload");
    }

    kmr_tests::test_retrieve_rpc_artifacts(soft_rpc, &hmac, &hmac);
}
