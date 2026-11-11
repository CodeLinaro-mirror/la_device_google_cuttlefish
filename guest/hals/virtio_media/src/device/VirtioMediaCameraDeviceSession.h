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

#include <ExternalCameraUtils.h>
#include <SimpleThread.h>
#include <aidl/android/hardware/camera/common/Status.h>
#include <aidl/android/hardware/camera/device/BnCameraDeviceSession.h>
#include <aidl/android/hardware/camera/device/BufferRequest.h>
#include <aidl/android/hardware/camera/device/Stream.h>
#include <android-base/unique_fd.h>
#include <android/hardware/graphics/mapper/2.0/IMapper.h>
#include <android/hardware/graphics/mapper/3.0/IMapper.h>
#include <android/hardware/graphics/mapper/4.0/IMapper.h>
#include <fmq/AidlMessageQueue.h>
#include <utils/Thread.h>
#include <deque>
#include <list>
#include <memory>
#include <vector>

#include "V4l2BufferMemory.h"

namespace android {
namespace hardware {
namespace camera {
namespace device {
namespace implementation {

class VirtioMediaCameraDeviceSession
    : public ::aidl::android::hardware::camera::device::BnCameraDeviceSession,
      public OutputThreadInterface {
 public:
  VirtioMediaCameraDeviceSession(
      const std::shared_ptr<
          ::aidl::android::hardware::camera::device::ICameraDeviceCallback>&
          callback,
      const ::android::hardware::camera::external::common::ExternalCameraConfig&
          cfg,
      const std::vector<SupportedV4L2Format>& sortedFormats,
      const CroppingType& croppingType,
      const common::V1_0::helper::CameraMetadata& chars,
      const std::string& cameraId, ::android::base::unique_fd v4l2Fd);
  ~VirtioMediaCameraDeviceSession() override;

  // Caller must use this method to check if CameraDeviceSession ctor failed
  bool isInitFailed();
  bool isClosed();

  ::ndk::ScopedAStatus close() override;

  ::ndk::ScopedAStatus configureStreams(
      const ::aidl::android::hardware::camera::device::StreamConfiguration&
          in_requestedConfiguration,
      std::vector<::aidl::android::hardware::camera::device::HalStream>*
          _aidl_return) override;
  ::ndk::ScopedAStatus constructDefaultRequestSettings(
      ::aidl::android::hardware::camera::device::RequestTemplate in_type,
      ::aidl::android::hardware::camera::device::CameraMetadata* _aidl_return)
      override;
  ::ndk::ScopedAStatus flush() override;
  ::ndk::ScopedAStatus getCaptureRequestMetadataQueue(
      ::aidl::android::hardware::common::fmq::MQDescriptor<
          int8_t,
          ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>*
          _aidl_return) override;
  ::ndk::ScopedAStatus getCaptureResultMetadataQueue(
      ::aidl::android::hardware::common::fmq::MQDescriptor<
          int8_t,
          ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>*
          _aidl_return) override;
  ::ndk::ScopedAStatus isReconfigurationRequired(
      const ::aidl::android::hardware::camera::device::CameraMetadata&
          in_oldSessionParams,
      const ::aidl::android::hardware::camera::device::CameraMetadata&
          in_newSessionParams,
      bool* _aidl_return) override;
  ::ndk::ScopedAStatus processCaptureRequest(
      const std::vector<
          ::aidl::android::hardware::camera::device::CaptureRequest>&
          in_requests,
      const std::vector<::aidl::android::hardware::camera::device::BufferCache>&
          in_cachesToRemove,
      int32_t* _aidl_return) override;
  ::ndk::ScopedAStatus signalStreamFlush(
      const std::vector<int32_t>& in_streamIds,
      int32_t in_streamConfigCounter) override;
  ::ndk::ScopedAStatus switchToOffline(
      const std::vector<int32_t>& in_streamsToKeep,
      ::aidl::android::hardware::camera::device::CameraOfflineSessionInfo*
          out_offlineSessionInfo,
      std::shared_ptr<
          ::aidl::android::hardware::camera::device::ICameraOfflineSession>*
          _aidl_return) override;
  ::ndk::ScopedAStatus repeatingRequestEnd(
      int32_t in_frameNumber,
      const std::vector<int32_t>& in_streamIds) override;

  ::aidl::android::hardware::camera::common::Status importBuffer(
      int32_t streamId, uint64_t bufId, buffer_handle_t buf,
      buffer_handle_t** outBufPtr) override;

  void notifyError(int32_t frameNumber, int32_t streamId,
                   ErrorCode ec) override;

  ::aidl::android::hardware::camera::common::Status processCaptureRequestError(
      const std::shared_ptr<HalRequest>& ptr, std::vector<NotifyMsg>* msgs,
      std::vector<CaptureResult>* results) override;

  ::aidl::android::hardware::camera::common::Status processCaptureResult(
      std::shared_ptr<HalRequest>& ptr) override;
  ssize_t getJpegBufferSize(int32_t width, int32_t height) const override;

  // Called by CameraDevice to dump active device states
  binder_status_t dump(int fd, const char** args, uint32_t numArgs) override;

  static ::aidl::android::hardware::camera::common::Status
  isStreamCombinationSupported(
      const ::aidl::android::hardware::camera::device::StreamConfiguration&
          config,
      const std::vector<SupportedV4L2Format>& supportedFormats,
      const ::android::hardware::camera::external::common::ExternalCameraConfig&
          devCfg);

  static const int kMaxProcessedStream = 2;
  static const int kMaxStallStream = 1;
  static const uint32_t kMaxBytesPerPixel = 3;

  class BufferRequestThread
      : public ::android::hardware::camera::common::helper::SimpleThread {
   public:
    BufferRequestThread(
        std::weak_ptr<OutputThreadInterface> parent,
        std::shared_ptr<
            ::aidl::android::hardware::camera::device::ICameraDeviceCallback>
            callbacks);

    int requestBufferStart(const std::vector<HalStreamBuffer>&);
    int waitForBufferRequestDone(
        /*out*/ std::vector<HalStreamBuffer>*);

    bool threadLoop() override;

   private:
    void waitForNextRequest();

    const std::weak_ptr<OutputThreadInterface> mParent;
    const std::shared_ptr<
        ::aidl::android::hardware::camera::device::ICameraDeviceCallback>
        mCallbacks;

    std::mutex mLock;
    bool mRequestingBuffer = false;

    std::vector<HalStreamBuffer> mBufferReqs;
    std::vector<HalStreamBuffer> mPendingReturnBufferReqs;
    // mHalBufferReqs is not under mLock protection during the HIDL transaction
    std::vector<::aidl::android::hardware::camera::device::BufferRequest>
        mHalBufferReqs;

    // request buffers takes much less time in steady state, but can take much
    // longer when requesting 1st buffer from a stream.
    // TODO: consider a separate timeout for new vs. steady state?
    // TODO: or make sure framework is warming up the pipeline during configure
    // new stream?
    static const int kReqProcTimeoutMs = 132;

    static const int kReqWaitTimeoutMs = 33;
    static const int kReqWaitTimesWarn = 90;  // 33ms * 90 ~= 3 sec
    std::condition_variable
        mRequestCond;  // signaled when a new buffer request incoming
    std::condition_variable
        mRequestDoneCond;  // signaled when a request is done
  };

  class OutputThread
      : public ::android::hardware::camera::common::helper::SimpleThread {
   public:
    OutputThread(std::weak_ptr<OutputThreadInterface> parent, CroppingType,
                 const common::V1_0::helper::CameraMetadata&,
                 std::shared_ptr<BufferRequestThread> bufReqThread);
    ~OutputThread();

    ::aidl::android::hardware::camera::common::Status
    allocateIntermediateBuffers(
        const Size& v4lSize, const Size& thumbSize,
        const std::vector<::aidl::android::hardware::camera::device::Stream>&
            streams,
        uint32_t blobBufferSize);
    ::aidl::android::hardware::camera::common::Status submitRequest(
        const std::shared_ptr<HalRequest>&);
    void flush();
    void dump(int fd);
    bool threadLoop() override;

    void setExifMakeModel(const std::string& make, const std::string& model);

    // The remaining request list is returned for offline processing
    std::list<std::shared_ptr<HalRequest>> switchToOffline();

   protected:
    static const int kFlushWaitTimeoutSec = 3;  // 3 sec
    static const int kReqWaitTimeoutMs = 33;    // 33ms
    static const int kReqWaitTimesMax = 90;     // 33ms * 90 ~= 3 sec

    // Methods to request output buffer in parallel
    int requestBufferStart(const std::vector<HalStreamBuffer>&);
    int waitForBufferRequestDone(
        /*out*/ std::vector<HalStreamBuffer>*);

    void waitForNextRequest(std::shared_ptr<HalRequest>* out);
    void signalRequestDone();

    int cropAndScaleLocked(
        std::shared_ptr<AllocatedFrame>& in, const Size& outSize,
        ::android::hardware::graphics::mapper::V2_0::YCbCrLayout* out);

    int cropAndScaleThumbLocked(
        std::shared_ptr<AllocatedFrame>& in, const Size& outSize,
        ::android::hardware::graphics::mapper::V2_0::YCbCrLayout* out);

    int createJpegLocked(HalStreamBuffer& halBuf,
                         const common::V1_0::helper::CameraMetadata& settings);

    void clearIntermediateBuffers();

    const std::weak_ptr<OutputThreadInterface> mParent;
    const CroppingType mCroppingType;
    const common::V1_0::helper::CameraMetadata mCameraCharacteristics;

    mutable std::mutex
        mRequestListLock;  // Protect access to mRequestList,
                           // mProcessingRequest and mProcessingFrameNumber
    std::condition_variable
        mRequestCond;  // signaled when a new request is submitted
    std::condition_variable
        mRequestDoneCond;  // signaled when a request is done processing
    std::list<std::shared_ptr<HalRequest>> mRequestList;
    bool mProcessingRequest = false;
    uint32_t mProcessingFrameNumber = 0;

    // V4L2 frameIn
    // (MJPG decode)-> mYu12Frame
    // (Scale)-> mScaledYu12Frames
    // (Format convert) -> output gralloc frames
    mutable std::mutex mBufferLock;  // Protect access to intermediate buffers
    std::shared_ptr<AllocatedFrame> mYu12Frame;
    std::shared_ptr<AllocatedFrame> mYu12ThumbFrame;
    std::unordered_map<
        Size, std::shared_ptr<AllocatedFrame>,
        ::android::hardware::camera::external::common::SizeHasher>
        mIntermediateBuffers;
    std::unordered_map<
        Size, std::shared_ptr<AllocatedFrame>,
        ::android::hardware::camera::external::common::SizeHasher>
        mScaledYu12Frames;
    ::android::hardware::graphics::mapper::V2_0::YCbCrLayout mYu12FrameLayout;
    ::android::hardware::graphics::mapper::V2_0::YCbCrLayout
        mYu12ThumbFrameLayout;
    std::vector<uint8_t> mMuteTestPatternFrame;
    uint32_t mTestPatternData[4] = {0, 0, 0, 0};
    bool mCameraMuted = false;
    uint32_t mBlobBufferSize =
        0;  // 0 -> HAL derive buffer size, else: use given size

    std::string mExifMake;
    std::string mExifModel;

    const std::shared_ptr<BufferRequestThread> mBufferRequestThread;
  };

 private:
  bool initialize();
  // To init/close different version of output thread
  void initOutputThread();
  void closeOutputThread();
  void closeBufferRequestThread();

  void closeImpl();
  ::aidl::android::hardware::camera::common::Status initStatus() const;
  status_t initDefaultRequests();

  status_t fillCaptureResult(common::V1_0::helper::CameraMetadata& md,
                             nsecs_t timestamp);
  int configureV4l2StreamLocked(const SupportedV4L2Format& fmt,
                                double fps = 0.0);
  ::aidl::android::hardware::camera::common::Status selectV4L2Format(
      const ::aidl::android::hardware::camera::device::StreamConfiguration&
          config,
      SupportedV4L2Format* outFmt);
  int v4l2StreamOffLocked();

  int setV4l2FpsLocked(double fps);

  std::unique_ptr<V4L2Frame> dequeueV4l2FrameLocked(
      /*out*/ nsecs_t* shutterTs);  // Called with mLock held

  void enqueueV4l2Frame(const std::shared_ptr<V4L2Frame>&);

  // Check if input Stream is one of supported stream setting on this device
  static bool isSupported(
      const ::aidl::android::hardware::camera::device::Stream& stream,
      const std::vector<SupportedV4L2Format>& supportedFormats,
      const ::android::hardware::camera::external::common::ExternalCameraConfig&
          cfg);

  ::aidl::android::hardware::camera::common::Status importBufferLocked(
      int32_t streamId, uint64_t bufId, buffer_handle_t buf,
      /*out*/ buffer_handle_t** outBufPtr);
  static void cleanupInflightFences(std::vector<int>& allFences,
                                    size_t numFences);
  void cleanupBuffersLocked(int id);

  void updateBufferCaches(
      const std::vector<::aidl::android::hardware::camera::device::BufferCache>&
          cachesToRemove);

  ::aidl::android::hardware::camera::common::Status processOneCaptureRequest(
      const ::aidl::android::hardware::camera::device::CaptureRequest& request);
  void notifyShutter(int32_t frameNumber, nsecs_t shutterTs);

  void invokeProcessCaptureResultCallback(std::vector<CaptureResult>& results,
                                          bool tryWriteFmq);
  Size getMaxJpegResolution() const;

  Size getMaxThumbResolution() const;

  int32_t getPartialResultCount() const;

  int waitForV4L2BufferReturnLocked(std::unique_lock<std::mutex>& lk);

  // Main body of switchToOffline. This method does not invoke any callbacks
  // but instead returns the necessary callbacks in output arguments so callers
  // can callback later without holding any locks
  ::aidl::android::hardware::camera::common::Status switchToOffline(
      const std::vector<int32_t>& offlineStreams,
      /*out*/ std::vector<NotifyMsg>* msgs,
      /*out*/ std::vector<CaptureResult>* results,
      /*out*/
      ::aidl::android::hardware::camera::device::CameraOfflineSessionInfo* info,
      /*out*/
      std::shared_ptr<
          ::aidl::android::hardware::camera::device::ICameraOfflineSession>*
          session);

  bool supportOfflineLocked(int32_t streamId);

  // Whether a request can be completely dropped when switching to offline
  bool canDropRequest(const std::vector<int32_t>& offlineStreams,
                      std::shared_ptr<HalRequest> halReq);

  void fillOfflineSessionInfo(
      const std::vector<int32_t>& offlineStreams,
      std::deque<std::shared_ptr<HalRequest>>& offlineReqs,
      const std::map<int, CirculatingBuffers>& circulatingBuffers,
      /*out*/
      ::aidl::android::hardware::camera::device::CameraOfflineSessionInfo*
          info);

  // Protect (most of) HIDL interface methods from synchronized-entering
  mutable Mutex mInterfaceLock;

  mutable Mutex mLock;  // Protect all private members except otherwise noted
  const std::shared_ptr<
      ::aidl::android::hardware::camera::device::ICameraDeviceCallback>
      mCallback;
  const ::android::hardware::camera::external::common::ExternalCameraConfig&
      mCfg;
  const common::V1_0::helper::CameraMetadata mCameraCharacteristics;
  const std::vector<SupportedV4L2Format> mSupportedFormats;
  const CroppingType mCroppingType;
  const std::string mCameraId;

  // Not protected by mLock, this is almost a const.
  // Setup in constructor, reset in close() after OutputThread is joined
  ::android::base::unique_fd mV4l2Fd;

  // device is closed either
  //    - closed by user
  //    - init failed
  //    - camera disconnected
  bool mClosed = false;
  bool mInitialized = false;
  bool mInitFail = false;
  bool mFirstRequest = false;
  common::V1_0::helper::CameraMetadata mLatestReqSetting;

  bool mV4l2Streaming = false;
  size_t mV4L2PlaneCount = 1;
  std::vector<uint32_t> mV4L2BytesPerLine;
  SupportedV4L2Format mV4l2StreamingFmt;
  double mV4l2StreamingFps = 0.0;
  size_t mV4L2BufferCount = 0;
  std::vector<V4L2BufferInfo> mV4L2Buffers;
  std::unique_ptr<V4l2BufferMemory> mV4l2Memory;

  static const int kBufferWaitTimeoutSec =
      3;                       // TODO: handle long exposure (or not allowing)
  std::mutex mV4l2BufferLock;  // protect the buffer count and condition below
  std::condition_variable mV4L2BufferReturned;
  size_t mNumDequeuedV4l2Buffers = 0;
  uint32_t mMaxV4L2BufferSize = 0;

  // Not protected by mLock (but might be used when mLock is locked)
  std::shared_ptr<OutputThread> mOutputThread;

  // Stream ID -> Stream cache
  std::unordered_map<int, ::aidl::android::hardware::camera::device::Stream>
      mStreamMap;

  std::mutex mInflightFramesLock;  // protect mInflightFrames
  std::unordered_set<uint32_t> mInflightFrames;

  // Stream ID -> circulating buffers map
  std::map<int, CirculatingBuffers> mCirculatingBuffers;
  // Protect mCirculatingBuffers, must not lock mLock after acquiring this lock
  mutable Mutex mCbsLock;

  std::mutex mAfTriggerLock;  // protect mAfTrigger
  bool mAfTrigger = false;

  int32_t mCurrentSensitivity = -1;

  uint32_t mBlobBufferSize = 0;

  static HandleImporter sHandleImporter;

  std::shared_ptr<BufferRequestThread> mBufferRequestThread;

  /* Beginning of members not changed after initialize() */
  using RequestMetadataQueue = ::android::AidlMessageQueue<
      int8_t, ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>;
  std::unique_ptr<RequestMetadataQueue> mRequestMetadataQueue;
  using ResultMetadataQueue = ::android::AidlMessageQueue<
      int8_t, ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>;
  std::shared_ptr<ResultMetadataQueue> mResultMetadataQueue;

  // Protect against invokeProcessCaptureResultCallback()
  Mutex mProcessCaptureResultLock;

  // tracks last seen stream config counter
  int32_t mLastStreamConfigCounter = -1;

  std::unordered_map<::aidl::android::hardware::camera::device::RequestTemplate,
                     ::aidl::android::hardware::camera::device::CameraMetadata>
      mDefaultRequests;

  const Size mMaxThumbResolution;
  const Size mMaxJpegResolution;

  std::string mExifMake;
  std::string mExifModel;
  /* End of members not changed after initialize() */

  // The max tolerant lag between the dequeued v4l2 buffer and current capture
  // request.
  uint64_t mMaxLagNs;
};

}  // namespace implementation
}  // namespace device
}  // namespace camera
}  // namespace hardware
}  // namespace android
