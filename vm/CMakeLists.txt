cmake_minimum_required(VERSION 3.16)
#
# Project details
#
project(
  "vm-bpf"
  VERSION 0.1.0
  LANGUAGES C CXX
)

if(NOT DEFINED BPFTIME_LLVM_JIT)
  set(BPFTIME_LLVM_JIT ON CACHE BOOL "Use LLVM as jit backend.")
endif()

if(NOT DEFINED BPFTIME_UBPF_JIT)
  set(BPFTIME_UBPF_JIT ON CACHE BOOL "Use uBPF as jit backend.")
endif()


if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE "Debug")
endif()

if(NOT DEFINED SPDLOG_INCLUDE)
  message(STATUS "Adding spdlog seperately..")
  # spdlog
  add_subdirectory(../third_party/spdlog ${CMAKE_CURRENT_BINARY_DIR}/spdlog)
  set(SPDLOG_INCLUDE ${CMAKE_CURRENT_SOURCE_DIR}/../third_party/spdlog/include)
endif()

if(${BPFTIME_BUILD_STANDALONE_VM})
  include(../cmake/libbpf.cmake)
  add_subdirectory(../third_party/argparse ${CMAKE_CURRENT_BINARY_DIR}/argparse)
endif()

if(${BPFTIME_LLVM_JIT})
  set(BPFTIME_BPF_RUNTIME_NAME "bpftime_llvm_vm")
  # Apply compatibility patch for modern LLVM/GCC (LLVM 21, GCC 15)
  execute_process(
    COMMAND git apply --check --ignore-whitespace ${CMAKE_CURRENT_SOURCE_DIR}/../cmake/llvm_jit_compat.patch
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/llvm-jit
    RESULT_VARIABLE PATCH_CHECK_RESULT
    OUTPUT_QUIET ERROR_QUIET
  )
  if(PATCH_CHECK_RESULT EQUAL 0)
    execute_process(
      COMMAND git apply --ignore-whitespace ${CMAKE_CURRENT_SOURCE_DIR}/../cmake/llvm_jit_compat.patch
      WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/llvm-jit
      RESULT_VARIABLE PATCH_RESULT
    )
    if(PATCH_RESULT EQUAL 0)
      message(STATUS "Applied llvm_jit_compat.patch to vm/llvm-jit")
    else()
      message(WARNING "Failed to apply llvm_jit_compat.patch: ${PATCH_RESULT}")
    endif()
  else()
    message(STATUS "llvm_jit_compat.patch already applied or not needed, skipping.")
  endif()
endif()

if(${BPFTIME_UBPF_JIT})
  set(BPFTIME_BPF_RUNTIME_NAME "bpftime_ubpf_vm")
endif()

if(NOT BPFTIME_BPF_RUNTIME_NAME)
  message(WARNING "No built-in VMs has been enabled.")
endif()

add_subdirectory(compat)
add_subdirectory(vm-core)

#
# Unit testing setup
#
set(test_sources
  src/test.c
  src/test_jit.c
  src/test_vm.c
  src/test_core_minimal_ffi.c
)

if(BPFTIME_ENABLE_UNIT_TESTING)
  enable_testing()
  message(STATUS "Build unit tests for the project. Tests should always be found in the test folder\n")
endif()
