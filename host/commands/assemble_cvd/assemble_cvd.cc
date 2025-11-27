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
#include <android-base/logging.h>
#include <gflags/gflags.h>

#include "common/libs/fs/shared_buf.h"
#include "common/libs/fs/shared_fd.h"
#include "common/libs/utils/environment.h"
#include "common/libs/utils/files.h"
#include "common/libs/utils/flag_parser.h"
#include "common/libs/utils/tee_logging.h"
#include "host/commands/assemble_cvd/clean.h"
#include "host/commands/assemble_cvd/disk_flags.h"
#include "host/commands/assemble_cvd/flag_feature.h"
#include "host/commands/assemble_cvd/flags.h"
#include "host/libs/config/adb/adb.h"
#include "host/libs/config/config_flag.h"
#include "host/libs/config/custom_actions.h"
#include "host/libs/config/fetcher_config.h"

using cuttlefish::StringFromEnv;

DEFINE_string(assembly_dir, StringFromEnv("HOME", ".") + "/cuttlefish_assembly",
              "A directory to put generated files common between instances");
DEFINE_string(instance_dir, StringFromEnv("HOME", ".") + "/cuttlefish",
              "This is a directory that will hold the cuttlefish generated"
              "files, including both instance-specific and common files");
DEFINE_bool(resume, true, "Resume using the disk from the last session, if "
                          "possible. i.e., if --noresume is passed, the disk "
                          "will be reset to the state it was initially launched "
                          "in. This flag is ignored if the underlying partition "
                          "images have been updated since the first launch.");
DEFINE_int32(modem_simulator_count, 1,
             "Modem simulator count corresponding to maximum sim number");

DECLARE_bool(use_overlay);

namespace cuttlefish {
namespace {

std::vector<std::string> SystemImageDirsFromArgs(
    std::vector<std::string> args) {
  std::string value;
  Flag flag = GflagsCompatFlag("system_image_dir", value);
  if (!flag.Parse(args)) {
    value = DefaultGuestImagePath("");
  }
  return android::base::Split(value, ",");
}

std::string kFetcherConfigFile = "fetcher_config.json";

FetcherConfigs FindFetcherConfigs(const std::vector<std::string>& args) {
  // The flags haven't been parsed yet, parse only the system_image_dir flag
  // from a copy of the arguments.
  std::vector<std::string> system_image_dirs = SystemImageDirsFromArgs(args);

  std::vector<FetcherConfig> fetcher_configs;
  for (size_t i = 0; i < system_image_dirs.size(); ++i) {
    std::string fetcher_file = system_image_dirs[i] + "/" + kFetcherConfigFile;
    FetcherConfig fetcher_config;
    if (!fetcher_config.LoadFromFile(fetcher_file)) {
      LOG(DEBUG) << "No valid fetcher_config in '" << fetcher_file
                 << "', falling back to default";
    }
    fetcher_configs.emplace_back(std::move(fetcher_config));
  }
  return FetcherConfigs::Create(std::move(fetcher_configs));
}

std::string GetLegacyConfigFilePath(const CuttlefishConfig& config) {
  return config.ForDefaultInstance().PerInstancePath("cuttlefish_config.json");
}

Result<void> SaveConfig(const CuttlefishConfig& tmp_config_obj) {
  auto config_file = GetConfigFilePath(tmp_config_obj);
  auto config_link = GetGlobalConfigFileLink();
  // Save the config object before starting any host process
  CF_EXPECT(tmp_config_obj.SaveToFile(config_file),
            "Failed to save to \"" << config_file << "\"");
  auto legacy_config_file = GetLegacyConfigFilePath(tmp_config_obj);
  CF_EXPECT(tmp_config_obj.SaveToFile(legacy_config_file),
            "Failed to save to \"" << legacy_config_file << "\"");

  setenv(kCuttlefishConfigEnvVarName, config_file.c_str(), true);
  if (symlink(config_file.c_str(), config_link.c_str()) != 0) {
    return CF_ERRNO("symlink(\"" << config_file << "\", \"" << config_link
                                 << ") failed");
  }

  return {};
}

#ifndef O_TMPFILE
# define O_TMPFILE (020000000 | O_DIRECTORY)
#endif

Result<void> CreateLegacySymlinks(
    const CuttlefishConfig::InstanceSpecific& instance) {
  std::string log_files[] = {
      "kernel.log",  "launcher.log",        "logcat",
      "metrics.log", "modem_simulator.log", "crosvm_openwrt.log",
  };
  for (const auto& log_file : log_files) {
    auto symlink_location = instance.PerInstancePath(log_file.c_str());
    auto log_target = "logs/" + log_file;  // Relative path
    if (symlink(log_target.c_str(), symlink_location.c_str()) != 0) {
      return CF_ERRNO("symlink(\"" << log_target << ", " << symlink_location
                                   << ") failed");
    }
  }

  std::stringstream legacy_instance_path_stream;
  legacy_instance_path_stream << FLAGS_instance_dir;
  if (gflags::GetCommandLineFlagInfoOrDie("instance_dir").is_default) {
    legacy_instance_path_stream << "_runtime";
  }
  legacy_instance_path_stream << "." << instance.id();
  auto legacy_instance_path = legacy_instance_path_stream.str();

  if (DirectoryExists(legacy_instance_path, /* follow_symlinks */ false)) {
    CF_EXPECT(RecursivelyRemoveDirectory(legacy_instance_path),
              "Failed to remove legacy directory " << legacy_instance_path);
  } else if (FileExists(legacy_instance_path, /* follow_symlinks */ false)) {
    CF_EXPECT(RemoveFile(legacy_instance_path),
              "Failed to remove instance_dir symlink " << legacy_instance_path);
  }
  if (symlink(instance.instance_dir().c_str(), legacy_instance_path.c_str())) {
    return CF_ERRNO("symlink(\"" << instance.instance_dir() << "\", \""
                                 << legacy_instance_path << "\") failed");
  }
  return {};
}

Result<const CuttlefishConfig*> InitFilesystemAndCreateConfig(
    FetcherConfigs fetcher_configs, KernelConfig kernel_config,
    fruit::Injector<>& injector) {
  std::string runtime_dir_parent = AbsolutePath(FLAGS_instance_dir);
  while (runtime_dir_parent[runtime_dir_parent.size() - 1] == '/') {
    runtime_dir_parent =
        runtime_dir_parent.substr(0, FLAGS_instance_dir.rfind('/'));
  }
  runtime_dir_parent =
      runtime_dir_parent.substr(0, FLAGS_instance_dir.rfind('/'));
  auto log = SharedFD::Open(runtime_dir_parent, O_WRONLY | O_TMPFILE,
                            S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP);
  if (!log->IsOpen()) {
    LOG(ERROR) << "Could not open O_TMPFILE precursor to assemble_cvd.log: "
               << log->StrError();
  } else {
    android::base::SetLogger(TeeLogger({
        {ConsoleSeverity(), SharedFD::Dup(2), MetadataLevel::ONLY_MESSAGE},
        {LogFileSeverity(), log, MetadataLevel::FULL},
    }));
  }

  {
    // The config object is created here, but only exists in memory until the
    // SaveConfig line below. Don't launch cuttlefish subprocesses between these
    // two operations, as those will assume they can read the config object from
    // disk.
    auto config = InitializeCuttlefishConfiguration(FLAGS_instance_dir,
                                                    FLAGS_modem_simulator_count,
                                                    kernel_config, injector);
    std::set<std::string> preserving;
    auto os_builder = OsCompositeDiskBuilder(config);
    bool creating_os_disk = CF_EXPECT(os_builder.WillRebuildCompositeDisk());
    // TODO(schuffelen): Add smarter decision for when to delete runtime files.
    // Files like NVChip are tightly bound to Android keymint and should be
    // deleted when userdata is reset. However if the user has ever run without
    // the overlay, then we want to keep this until userdata.img was externally
    // replaced.
    creating_os_disk &= FLAGS_use_overlay;
    if (FLAGS_resume && creating_os_disk) {
      LOG(INFO) << "Requested resuming a previous session (the default behavior) "
                << "but the base images have changed under the overlay, making the "
                << "overlay incompatible. Wiping the overlay files.";
    } else if (FLAGS_resume && !creating_os_disk) {
      preserving.insert("overlay.img");
      preserving.insert("ap_overlay.img");
      preserving.insert("os_composite_disk_config.txt");
      preserving.insert("os_composite_gpt_header.img");
      preserving.insert("os_composite_gpt_footer.img");
      preserving.insert("os_composite.img");
      preserving.insert("sdcard.img");
      preserving.insert("boot_repacked.img");
      preserving.insert("vendor_boot_repacked.img");
      preserving.insert("access-kregistry");
      preserving.insert("hwcomposer-pmem");
      preserving.insert("NVChip");
      preserving.insert("gatekeeper_secure");
      preserving.insert("gatekeeper_insecure");
      preserving.insert("modem_nvram.json");
      preserving.insert("recording");
      preserving.insert("persistent_composite_disk_config.txt");
      preserving.insert("persistent_composite_gpt_header.img");
      preserving.insert("persistent_composite_gpt_footer.img");
      preserving.insert("persistent_composite.img");
      preserving.insert("uboot_env.img");
      preserving.insert("factory_reset_protected.img");
      std::stringstream ss;
      for (int i = 0; i < FLAGS_modem_simulator_count; i++) {
        ss.clear();
        ss << "iccprofile_for_sim" << i << ".xml";
        preserving.insert(ss.str());
        ss.str("");
      }
    }
    CF_EXPECT(CleanPriorFiles(preserving, config.assembly_dir(),
                              config.instance_dirs()),
              "Failed to clean prior files");

    CF_EXPECT(EnsureDirectoryExists(config.root_dir()));
    CF_EXPECT(EnsureDirectoryExists(config.assembly_dir()));
    CF_EXPECT(EnsureDirectoryExists(config.instances_dir()));
    if (log->LinkAtCwd(config.AssemblyPath("assemble_cvd.log"))) {
      LOG(ERROR) << "Unable to persist assemble_cvd log at "
                  << config.AssemblyPath("assemble_cvd.log")
                  << ": " << log->StrError();
    }

    auto disk_config = GetOsCompositeDiskConfig();
    if (auto it = std::find_if(disk_config.begin(), disk_config.end(),
                               [](const auto& partition) {
                                 return partition.label == "ap_rootfs";
                               });
        it != disk_config.end()) {
      auto ap_image_idx = std::distance(disk_config.begin(), it) + 1;
      std::stringstream ss;
      ss << "/dev/vda" << ap_image_idx;
      config.set_ap_image_dev_path(ss.str());
    }
    for (const auto& instance : config.Instances()) {
      // Create instance directory if it doesn't exist.
      CF_EXPECT(EnsureDirectoryExists(instance.instance_dir()));
      auto internal_dir = instance.instance_dir() + "/" + kInternalDirName;
      CF_EXPECT(EnsureDirectoryExists(internal_dir));
      auto shared_dir = instance.instance_dir() + "/" + kSharedDirName;
      CF_EXPECT(EnsureDirectoryExists(shared_dir));
      auto recording_dir = instance.instance_dir() + "/recording";
      CF_EXPECT(EnsureDirectoryExists(recording_dir));
      CF_EXPECT(EnsureDirectoryExists(instance.PerInstanceLogPath("")));
      // TODO(schuffelen): Move this code somewhere better
      CF_EXPECT(CreateLegacySymlinks(instance));
    }
    CF_EXPECT(SaveConfig(config), "Failed to initialize configuration");
  }

  // Do this early so that the config object is ready for anything that needs it
  auto config = CuttlefishConfig::Get();
  CF_EXPECT(config != nullptr, "Failed to obtain config singleton");

  if (DirectoryExists(FLAGS_assembly_dir, /* follow_symlinks */ false)) {
    CF_EXPECT(RecursivelyRemoveDirectory(FLAGS_assembly_dir),
              "Failed to remove directory " << FLAGS_assembly_dir);
  } else if (FileExists(FLAGS_assembly_dir, /* follow_symlinks */ false)) {
    CF_EXPECT(RemoveFile(FLAGS_assembly_dir),
              "Failed to remove file" << FLAGS_assembly_dir);
  }
  if (symlink(config->assembly_dir().c_str(), FLAGS_assembly_dir.c_str())) {
    return CF_ERRNO("symlink(\"" << config->assembly_dir() << "\", \""
                                 << FLAGS_assembly_dir << "\") failed");
  }

  std::string first_instance = config->Instances()[0].instance_dir();
  std::string double_legacy_instance_dir = FLAGS_instance_dir + "_runtime";
  if (FileExists(double_legacy_instance_dir, /* follow_symlinks */ false)) {
    CF_EXPECT(RemoveFile(double_legacy_instance_dir),
              "Failed to remove symlink " << double_legacy_instance_dir);
  }
  if (symlink(first_instance.c_str(), double_legacy_instance_dir.c_str())) {
    return CF_ERRNO("symlink(\"" << first_instance << "\", \""
                                 << double_legacy_instance_dir << "\") failed");
  }

  CF_EXPECT(CreateDynamicDiskFiles(fetcher_configs, *config));

  return config;
}

const std::string kKernelDefaultPath = "kernel";
const std::string kInitramfsImg = "initramfs.img";
static void ExtractKernelParamsFromFetcherConfig(
    const FetcherConfigs& fetcher_configs) {
  std::vector<std::string> discovered_kernel_files;
  std::vector<std::string> discovered_initramfs_files;
  for (size_t i = 0; i < fetcher_configs.Size(); i++) {
    const FetcherConfig& fetcher_config = fetcher_configs.ForInstance(i);
    discovered_kernel_files.emplace_back(
        fetcher_config.FindCvdFileWithSuffix(kKernelDefaultPath));
    discovered_initramfs_files.emplace_back(
        fetcher_config.FindCvdFileWithSuffix(kInitramfsImg));
  }

  std::string kernel_arg = android::base::Join(discovered_kernel_files, ",");
  SetCommandLineOptionWithMode("kernel_path", kernel_arg.c_str(),
                               google::FlagSettingMode::SET_FLAGS_DEFAULT);

  std::string initramfs_arg =
      android::base::Join(discovered_initramfs_files, ",");
  SetCommandLineOptionWithMode("initramfs_path", initramfs_arg.c_str(),
                               google::FlagSettingMode::SET_FLAGS_DEFAULT);
}

fruit::Component<> FlagsComponent() {
  return fruit::createComponent()
      .install(AdbConfigComponent)
      .install(AdbConfigFlagComponent)
      .install(AdbConfigFragmentComponent)
      .install(GflagsComponent)
      .install(ConfigFlagComponent)
      .install(CustomActionsComponent);
}

Result<void> CheckNoTTY() {
  int tty = isatty(0);
  int error_num = errno;
  CF_EXPECT(tty == 0,
            "stdin was a tty, expected to be passed the output of a "
            "previous stage. Did you mean to run launch_cvd?");
  CF_EXPECT(error_num != EBADF,
            "stdin was not a valid file descriptor, expected to be "
            "passed the output of launch_cvd. Did you mean to run launch_cvd?");
  return {};
}

Result<std::vector<std::string>> ReadInputFiles() {
  std::string input_files_str;
  auto input_fd = SharedFD::Dup(0);
  CF_EXPECT(input_fd->IsOpen(),
            "Failed to dup stdin: " << input_fd->StrError());
  auto bytes_read = ReadAll(input_fd, &input_files_str);
  CF_EXPECT(bytes_read >= 0, "Failed to read input files. Error was \""
                                 << input_fd->StrError() << "\"");
  return android::base::Split(input_files_str, "\n");
}

}  // namespace

Result<int> AssembleCvdMain(int argc, char** argv) {
  setenv("ANDROID_LOG_TAGS", "*:v", /* overwrite */ 0);
  ::android::base::InitLogging(argv, android::base::StderrLogger);

  CF_EXPECT(CheckNoTTY());

  // Read everything that cvd_internal_start writes, but ignore it since
  // fetcher_config.json will be searched for in the system image directory.
  (void)CF_EXPECT(ReadInputFiles());

  auto args = ArgsToVec(argc - 1, argv + 1);

  FetcherConfigs fetcher_configs = FindFetcherConfigs(args);

  // set gflags defaults to point to kernel/RD from fetcher config
  ExtractKernelParamsFromFetcherConfig(fetcher_configs);

  bool help = false;
  std::string help_str;
  bool helpxml = false;

  std::vector<Flag> help_flags = {
      GflagsCompatFlag("help", help),
      GflagsCompatFlag("helpfull", help),
      GflagsCompatFlag("helpshort", help),
      GflagsCompatFlag("helpmatch", help_str),
      GflagsCompatFlag("helpon", help_str),
      GflagsCompatFlag("helppackage", help_str),
      GflagsCompatFlag("helpxml", helpxml),
  };
  for (const auto& help_flag : help_flags) {
    if (!help_flag.Parse(args)) {
      LOG(ERROR) << "Failed to process help flag.";
      return 1;
    }
  }

  fruit::Injector<> injector(FlagsComponent);
  auto flag_features = injector.getMultibindings<FlagFeature>();
  CF_EXPECT(FlagFeature::ProcessFlags(flag_features, args),
            "Failed to parse flags.");

  if (help || help_str != "") {
    LOG(WARNING) << "TODO(schuffelen): Implement `--help` for assemble_cvd.";
    LOG(WARNING) << "In the meantime, call `launch_cvd --help`";
    return 1;
  } else if (helpxml) {
    if (!FlagFeature::WriteGflagsHelpXml(flag_features, std::cout)) {
      LOG(ERROR) << "Failure in writing gflags helpxml output";
    }
    std::exit(1);  // For parity with gflags
  }
  // TODO(schuffelen): Put in "unknown flag" guards after gflags is removed.
  // gflags either consumes all arguments that start with - or leaves all of
  // them in place, and either errors out on unknown flags or accepts any flags.

  auto kernel_config =
      CF_EXPECT(GetKernelConfigAndSetDefaults(), "Failed to parse arguments");

  auto config = CF_EXPECT(InitFilesystemAndCreateConfig(
                              std::move(fetcher_configs),
                              kernel_config, injector),
                          "Failed to create config");

  std::cout << GetConfigFilePath(*config) << "\n";
  std::cout << std::flush;

  return 0;
}

} // namespace cuttlefish

int main(int argc, char** argv) {
  auto res = cuttlefish::AssembleCvdMain(argc, argv);
  CHECK(res.ok()) << "assemble_cvd failed: \n" << res.error();
  return *res;
}
