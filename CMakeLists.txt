cmake_minimum_required(VERSION 3.22)

project(reflect LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(ANDROID)
  add_subdirectory(protocol)
  add_subdirectory(android-runtime)
elseif(APPLE)
  enable_language(OBJCXX)
  add_subdirectory(macos)
else()
  message(FATAL_ERROR "Reflect targets Apple silicon macOS and Android ARM64")
endif()
