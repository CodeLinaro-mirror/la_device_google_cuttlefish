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

#define LOG_TAG "VirtioMediaV4l2BufMem"
#include <log/log.h>

#include "V4l2BufferMemory.h"

#include <sys/mman.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

namespace android {
namespace hardware {
namespace camera {
namespace device {
namespace implementation {

void* MmapBufferMemory::Attach(uint32_t index, uint32_t plane,
                               v4l2_plane* desc) {
  void* addr = mmap(nullptr, desc->length, PROT_READ | PROT_WRITE, MAP_SHARED,
                    device_fd_, desc->m.mem_offset);
  if (addr == MAP_FAILED) {
    ALOGE("%s: mmap buffer %u plane %u failed: %s", __FUNCTION__, index, plane,
          strerror(errno));
    return nullptr;
  }
  mappings_.emplace_back(addr, desc->length);
  return addr;
}

UserPtrBufferMemory::UserPtrBufferMemory(uint32_t buffer_count,
                                         uint32_t plane_count)
    : buffers_(buffer_count) {
  for (Buffer& buffer : buffers_) {
    buffer.planes.resize(plane_count);
  }
}

void* UserPtrBufferMemory::Attach(uint32_t index, uint32_t plane,
                                  v4l2_plane* desc) {
  LOG_ALWAYS_FATAL_IF(index >= buffers_.size(), "buffer %u out of range (%zu)",
                      index, buffers_.size());
  LOG_ALWAYS_FATAL_IF(plane >= buffers_[index].planes.size(),
                      "buffer %u plane %u out of range (%zu)", index, plane,
                      buffers_[index].planes.size());

  void* addr = nullptr;
  // Page-aligned so the plane spans as few guest-physical ranges as possible
  // in the driver's sg-list.
  const size_t page_size = static_cast<size_t>(sysconf(_SC_PAGESIZE));
  if (posix_memalign(&addr, page_size, desc->length) != 0) {
    ALOGE("%s: allocating %u bytes for buffer %u plane %u failed", __FUNCTION__,
          desc->length, index, plane);
    return nullptr;
  }
  // Touch every page so the memory is resident before the device writes to it.
  memset(addr, 0, desc->length);

  buffers_[index].planes[plane].memory.reset(static_cast<uint8_t*>(addr));
  desc->m.userptr = reinterpret_cast<unsigned long>(addr);
  return addr;
}

}  // namespace implementation
}  // namespace device
}  // namespace camera
}  // namespace hardware
}  // namespace android
