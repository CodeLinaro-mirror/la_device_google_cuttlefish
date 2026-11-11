#
# Copyright (C) 2024 The Android Open Source Project
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
#

# Enabling fullaccess for clients to omapi in cuttlefish
PRODUCT_PRODUCT_PROPERTIES += \
    persist.service.seek=fullaccess

PRODUCT_COPY_FILES += \
    device/google/cuttlefish/shared/omapi/hal_uuid_map_config.xml:$(TARGET_COPY_OUT_VENDOR)/etc/hal_uuid_map_config.xml \
    frameworks/native/data/etc/android.hardware.se.omapi.ese.xml:$(TARGET_COPY_OUT_VENDOR)/etc/permissions/android.hardware.se.omapi.ese.xml

# Enable Java OMAPI if Native OMAPI is not enabled.
# Also check RELEASE_NATIVE_OMAPI* directly because some product makefiles
# (e.g., cf_x86_64_tablet.mk) inherit shared/phone/device_vendor.mk before
# base_system.mk, where NATIVE_OMAPI is assigned.
ifeq (,$(filter true,$(NATIVE_OMAPI) $(RELEASE_NATIVE_OMAPI) $(RELEASE_NATIVE_OMAPI_CUTTLEFISH)))
# Enable Java OMAPI
DEVICE_PACKAGE_OVERLAYS += \
    device/google/cuttlefish/shared/omapi/overlay

PRODUCT_PACKAGES += secure_element_framework_matrix.xml
endif
