//
// Copyright (C) 2019 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <iostream>

#include <android-base/strings.h>
#include <glog/logging.h>
#include <gflags/gflags.h>

#include "common/libs/fs/shared_buf.h"
#include "common/libs/fs/shared_fd.h"
#include "common/libs/utils/environment.h"
#include "common/libs/utils/files.h"
#include "host/commands/assemble_cvd/assembler_defs.h"
#include "host/commands/assemble_cvd/flags.h"
#include "host/libs/config/fetcher_config.h"

DECLARE_string(system_image_dir);

namespace {

std::string kFetcherConfigFile = "fetcher_config.json";

cuttlefish::FetcherConfigs FindFetcherConfigs() {
  std::string system_image_dir_flag = FLAGS_system_image_dir;
  if (system_image_dir_flag.empty()) {
    system_image_dir_flag = cuttlefish::DefaultGuestImagePath("");
  }
  std::vector<std::string> system_image_dirs =
      android::base::Split(system_image_dir_flag, ",");

  std::vector<cuttlefish::FetcherConfig> fetcher_configs;
  for (size_t i = 0; i < system_image_dirs.size(); ++i) {
    std::string fetcher_file = system_image_dirs[i] + "/" + kFetcherConfigFile;
    cuttlefish::FetcherConfig fetcher_config;
    if (!fetcher_config.LoadFromFile(fetcher_file)) {
      LOG(DEBUG) << "No valid fetcher_config in '" << fetcher_file
                 << "', falling back to default";
    }
    fetcher_configs.emplace_back(std::move(fetcher_config));
  }
  return cuttlefish::FetcherConfigs::Create(std::move(fetcher_configs));
}

} // namespace

int main(int argc, char** argv) {
  setenv("ANDROID_LOG_TAGS", "*:v", /* overwrite */ 0);
  ::android::base::InitLogging(argv, android::base::StderrLogger);

  if (isatty(0)) {
    LOG(FATAL) << "stdin was a tty, expected to be passed the output of a previous stage. "
               << "Did you mean to run launch_cvd?";
    return cuttlefish::AssemblerExitCodes::kInvalidHostConfiguration;
  } else {
    int error_num = errno;
    if (error_num == EBADF) {
      LOG(FATAL) << "stdin was not a valid file descriptor, expected to be passed the output "
                 << "of launch_cvd. Did you mean to run launch_cvd?";
      return cuttlefish::AssemblerExitCodes::kInvalidHostConfiguration;
    }
  }

  std::string input_files_str;
  {
    auto input_fd = cuttlefish::SharedFD::Dup(0);
    auto bytes_read = cuttlefish::ReadAll(input_fd, &input_files_str);
    if (bytes_read < 0) {
      LOG(FATAL) << "Failed to read input files. Error was \"" << input_fd->StrError() << "\"";
    }
  }
  std::vector<std::string> input_files = android::base::Split(input_files_str, "\n");

  (void)input_files;
  auto config = InitFilesystemAndCreateConfig(&argc, &argv,
                                              FindFetcherConfigs());

  std::cout << GetConfigFilePath(*config) << "\n";
  std::cout << std::flush;

  return cuttlefish::AssemblerExitCodes::kSuccess;
}
