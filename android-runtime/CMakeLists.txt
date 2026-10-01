add_library(openxr_runtime SHARED
  openxr_dispatch/runtime_entry.cpp
  session.cpp
  pose_client.cpp
  game_patches.cpp
  swapchain.cpp
  input.cpp
  vulkan_backend.cpp
)

target_include_directories(openxr_runtime
  PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${PROJECT_SOURCE_DIR}/protocol
)

target_link_libraries(openxr_runtime
  PRIVATE
    refract_protocol
)

if(ANDROID)
  target_sources(openxr_runtime PRIVATE video_encoder.cpp gles_share.cpp)
  target_compile_definitions(openxr_runtime PRIVATE VK_USE_PLATFORM_ANDROID_KHR)
  target_link_libraries(openxr_runtime PRIVATE log GLESv3 EGL vulkan mediandk android nativewindow)
endif()

set_target_properties(openxr_runtime PROPERTIES
  OUTPUT_NAME openxr_runtime
)
