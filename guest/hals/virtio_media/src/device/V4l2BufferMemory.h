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

#pragma once

#include <linux/videodev2.h>

#include <cstdint>
#include <cstdlib>
#include <memory>
#include <vector>

#include <ExternalCameraUtils.h>

namespace android {
namespace hardware {
namespace camera {
namespace device {
namespace implementation {

// Interface for the V4L2 memory type (enum v4l2_memory) backing the capture
// buffers.
class V4l2BufferMemory {
 public:
  virtual ~V4l2BufferMemory() = default;

  virtual v4l2_memory V4l2Type() const = 0;

  // Provides the memory for plane `plane` of buffer `index` and returns its
  // address. `desc` contains the plane info returned by VIDIOC_QUERYBUF.
  virtual void* Attach(uint32_t index, uint32_t plane, v4l2_plane* desc) = 0;

  // Releases the buffers' memory from the device.
  virtual void Detach() = 0;
};

// V4L2_MEMORY_MMAP: the memory is device-owned and mapped into the HAL.
class MmapBufferMemory final : public V4l2BufferMemory {
 public:
  explicit MmapBufferMemory(int device_fd) : device_fd_(device_fd) {}

  v4l2_memory V4l2Type() const override { return V4L2_MEMORY_MMAP; }
  void* Attach(uint32_t index, uint32_t plane, v4l2_plane* desc) override;
  void Detach() override { mappings_.clear(); }

 private:
  const int device_fd_;
  std::vector<V4L2MappedBuffer> mappings_;
};

// V4L2_MEMORY_USERPTR: the memory is HAL-owned, page-aligned, and written into
// by the device.
class UserPtrBufferMemory final : public V4l2BufferMemory {
 public:
  UserPtrBufferMemory(uint32_t buffer_count, uint32_t plane_count);

  v4l2_memory V4l2Type() const override { return V4L2_MEMORY_USERPTR; }
  void* Attach(uint32_t index, uint32_t plane, v4l2_plane* desc) override;
  void Detach() override {}

 private:
  struct Plane {
    std::unique_ptr<uint8_t, decltype(&std::free)> memory{nullptr, &std::free};
  };
  struct Buffer {
    std::vector<Plane> planes;
  };
  // Indexed by v4l2_buffer.index.
  std::vector<Buffer> buffers_;
};

}  // namespace implementation
}  // namespace device
}  // namespace camera
}  // namespace hardware
}  // namespace android
