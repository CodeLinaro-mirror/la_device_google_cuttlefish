LOCAL_PATH := $(call my-dir)

cvd_host_package_tar := $(HOST_OUT)/cvd-host_package.tar.gz

.PHONY: hosttar
hosttar: $(cvd_host_package_tar)

# Build this by default when a developer types make
droidcore: $(cvd_host_package_tar)

# Build and store them on the build server.
$(call dist-for-goals, dist_files, $(cvd_host_package_tar))

bin_path := $(notdir $(HOST_OUT_EXECUTABLES))
lib_path := $(notdir $(HOST_OUT_SHARED_LIBRARIES))
tests_path := $(notdir $(HOST_OUT_NATIVE_TESTS))
webrtc_files_path := usr/share/webrtc
modem_simulator_path := etc/modem_simulator
cvd_custom_action_config_path := etc/cvd_custom_action_config

cvd_host_executables := \
    adb \
    adbshell \
    detect_graphics \
    launch_cvd \
    build_super_image \
    lpmake \
    lpunpack \
    socket_vsock_proxy \
    adb_connector \
    stop_cvd \
    vnc_server \
    cf_bpttool \
    kernel_log_monitor \
    extract-vmlinux \
    crosvm \
    aarch64-linux-gnu/crosvm \
    aarch64-linux-gnu/libdrm.so.2 \
    aarch64-linux-gnu/libepoxy.so.0 \
    aarch64-linux-gnu/libgbm.so.1 \
    aarch64-linux-gnu/libminijail.so \
    aarch64-linux-gnu/libvirglrenderer.so.1 \
    x86_64-linux-gnu/crosvm \
    x86_64-linux-gnu/libdrm.so.2 \
    x86_64-linux-gnu/libepoxy.so.0 \
    x86_64-linux-gnu/libgbm.so.1 \
    x86_64-linux-gnu/libgfxstream_backend.so \
    x86_64-linux-gnu/libminijail.so \
    x86_64-linux-gnu/libvirglrenderer.so.1 \
    logcat_receiver \
    config_server \
    tombstone_receiver \
    console_forwarder \
    assemble_cvd \
    run_cvd \
    cvd_internal_host_bugreport \
    cvd_internal_status \
    powerwash_cvd \
    mkbootimg \
    unpack_bootimg \
    webRTC \
    webrtc_operator \
    fsck.f2fs \
    resize.f2fs \
    make_f2fs \
    lz4 \
    tapsetiff \
    newfs_msdos \
    modem_simulator \
    secure_env \
    mkenvimage \
    log_tee \

ifneq ($(wildcard device/google/trout),)
    cvd_host_executables += android.hardware.automotive.vehicle@2.0-virtualization-grpc-server
endif

cvd_custom_action_config :=
ifneq ($(SOONG_CONFIG_cvd_custom_action_config),)
    cvd_custom_action_config := $(cvd_custom_action_config_path)/$(SOONG_CONFIG_cvd_custom_action_config)
    cvd_host_executables += $(SOONG_CONFIG_cvd_custom_action_servers)
endif

cvd_host_tests := \
    cuttlefish_net_tests \
    modem_simulator_test \

cvd_host_shared_libraries := \
    libbase.so \
    libcutils.so \
    libcuttlefish_device_config.so \
    libcuttlefish_device_config_proto.so \
    libcuttlefish_fs.so \
    libcuttlefish_kernel_log_monitor_utils.so \
    libcuttlefish_utils.so \
    cuttlefish_net.so \
    liblog.so \
    libnl.so \
    libc++.so \
    liblp.so \
    libsparse-host.so \
    libcrypto-host.so \
    libcrypto_utils.so \
    libext4_utils.so \
    libz-host.so \
    libicuuc-host.so \
    libicui18n-host.so \
    libandroidicu-host.so \
    libprotobuf-cpp-full.so \
    libziparchive.so \
    libvpx.so \
    libssl-host.so \
    libopus.so \
    libyuv.so \
    libjpeg.so \
    libhidlbase.so \
    libutils.so \
    libjsoncpp.so \
    libgrpc++.so \
    android.hardware.automotive.vehicle@2.0.so \
    libxml2.so \
    libkeymaster_messages.so \
    libkeymaster_portable.so \
    libsoft_attestation_cert.so \
    libcuttlefish_security.so \
    tpm2-tss2-esys.so \
    tpm2-tss2-mu.so \
    tpm2-tss2-rc.so \
    tpm2-tss2-sys.so \
    tpm2-tss2-tcti.so \
    tpm2-tss2-util.so \
    libgatekeeper.so \
    ms-tpm-20-ref-lib.so \
    libpuresoftkeymasterdevice_host.so

webrtc_assets := \
    index.html \
    style.css \
    js/adb.js \
    js/app.js \
    js/cf_webrtc.js \

webrtc_certs := \
    server.crt \
    server.key \
    server.p12 \
    trusted.pem \

cvd_host_webrtc_files := \
    $(addprefix assets/,$(webrtc_assets)) \
    $(addprefix certs/,$(webrtc_certs)) \

modem_simulator_files := \
     iccprofile_for_sim0.xml \
     iccprofile_for_sim0_for_CtsCarrierApiTestCases.xml \
     numeric_operator.xml \

cvd_host_package_files := \
     $(addprefix $(bin_path)/,$(cvd_host_executables)) \
     $(addprefix $(lib_path)/,$(cvd_host_shared_libraries)) \
     $(foreach test,$(cvd_host_tests), ${tests_path}/$(test)/$(test)) \
     $(addprefix $(webrtc_files_path)/,$(cvd_host_webrtc_files)) \
     $(addprefix $(modem_simulator_path)/files/,$(modem_simulator_files)) \
     $(cvd_custom_action_config) \

$(cvd_host_package_tar): PRIVATE_FILES := $(cvd_host_package_files)
$(cvd_host_package_tar): $(addprefix $(HOST_OUT)/,$(cvd_host_package_files))
	$(hide) rm -rf $@ && tar Scfz $@.tmp -C $(HOST_OUT) $(PRIVATE_FILES)
	$(hide) mv $@.tmp $@
