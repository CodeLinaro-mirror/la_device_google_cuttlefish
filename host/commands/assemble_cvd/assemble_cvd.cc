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

#include "common/libs/fs/shared_buf.h"
#include "common/libs/fs/shared_fd.h"
#include "common/libs/utils/environment.h"
#include "common/libs/utils/files.h"
#include "host/commands/assemble_cvd/assembler_defs.h"
#include "host/commands/assemble_cvd/flags.h"
#include "host/libs/config/fetcher_config.h"

namespace {

std::string kFetcherConfigFile = "fetcher_config.json";

struct LocatedFetcherConfig {
  cuttlefish::FetcherConfig fetcher_config;
  std::optional<std::string> working_dir;
};

LocatedFetcherConfig FindFetcherConfig(const std::vector<std::string>& files) {
  LocatedFetcherConfig located_fetcher_config;
  for (const auto& file : files) {
    if (android::base::EndsWith(file, kFetcherConfigFile)) {
      std::string home_directory =
          cuttlefish::StringFromEnv("HOME", cuttlefish::CurrentDirectory());
      std::string fetcher_file = file;
      if (!cuttlefish::FileExists(file) &&
          cuttlefish::FileExists(home_directory + "/" + fetcher_file)) {
        LOG(INFO) << "Found " << fetcher_file << " in HOME directory ('"
                  << home_directory << "') and not current working directory";

        located_fetcher_config.working_dir = home_directory;
        fetcher_file = home_directory + "/" + fetcher_file;
      }

      if (located_fetcher_config.fetcher_config.LoadFromFile(fetcher_file)) {
        return located_fetcher_config;
      }
      LOG(ERROR) << "Could not load fetcher config file.";
    }
  }
  return located_fetcher_config;
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

  LocatedFetcherConfig located_fetcher_config = FindFetcherConfig(input_files);
  if (located_fetcher_config.working_dir) {
    LOG(INFO) << "Changing current working dircetory to '"
              << *located_fetcher_config.working_dir << "'";
    CHECK(chdir((*located_fetcher_config.working_dir).c_str()) == 0)
        << "Unable to change working dir to '"
        << *located_fetcher_config.working_dir << "': " << strerror(errno);
  }

  auto config = InitFilesystemAndCreateConfig(
      &argc, &argv, std::move(located_fetcher_config.fetcher_config));

  std::cout << GetConfigFilePath(*config) << "\n";
  std::cout << std::flush;

  return cuttlefish::AssemblerExitCodes::kSuccess;
}
