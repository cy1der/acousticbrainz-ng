cmake_minimum_required(VERSION 3.18)

project(abng_extractors LANGUAGES C CXX)

# Builds the Essentia based tools used by the plugin. Use the scripts in the
# scripts/ directory instead of calling this directly, they take care of the
# dependencies.

set(ESSENTIA_SOURCE_DIR "" CACHE PATH "Path to the Essentia source tree (https://github.com/wo80/essentia, cmake branch)")

if(NOT EXISTS "${ESSENTIA_SOURCE_DIR}/CMakeLists.txt")
  message(FATAL_ERROR "ESSENTIA_SOURCE_DIR must point to the Essentia source tree")
endif()

# Only build the parts of Essentia needed by the tools
set(BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(BUILD_PYTHON_BINDINGS OFF CACHE BOOL "" FORCE)
set(BUILD_VAMP_PLUGIN OFF CACHE BOOL "" FORCE)
set(USE_TENSORFLOW ON CACHE BOOL "" FORCE)
set(USE_GAIA2 OFF CACHE BOOL "" FORCE)
set(DEBUGGING_ENABLED OFF CACHE BOOL "" FORCE)
set(STRIP_DOCUMENTATION ON CACHE BOOL "" FORCE)

# Never pick up optional libraries from the system, the tools don't need them
# and they would make the binaries depend on the system's versions
set(CMAKE_DISABLE_FIND_PACKAGE_Taglib ON)
set(CMAKE_DISABLE_FIND_PACKAGE_Chromaprint ON)
set(CMAKE_DISABLE_FIND_PACKAGE_VampSdk ON)
set(CMAKE_DISABLE_FIND_PACKAGE_Gaia2 ON)

add_subdirectory("${ESSENTIA_SOURCE_DIR}" essentia EXCLUDE_FROM_ALL)

# This generated file registers all of Essentia's algorithms in one huge
# function, which MSVC takes hours to optimize. It only runs once at startup,
# so it doesn't need to be optimized
if(MSVC)
  set_source_files_properties("${ESSENTIA_SOURCE_DIR}/src/algorithms/essentia_algorithms_reg_2.cpp"
    DIRECTORY "${ESSENTIA_SOURCE_DIR}/src"
    PROPERTIES COMPILE_OPTIONS "/Od")
endif()

# Essentia silently disables the algorithms whose dependencies are missing,
# which would only show up as errors at runtime. The find_library() results
# are cache variables, so they are visible here
foreach(library AVCODEC_LIBRARIES AVFORMAT_LIBRARIES AVUTIL_LIBRARIES SWRESAMPLE_LIBRARIES
                FFTW3f_LIBRARY yaml_LIBRARY SampleRate_LIBRARY TensorFlow_LIBRARY)
  if(NOT ${library})
    message(FATAL_ERROR "Required dependency not found: ${library}")
  endif()
endforeach()

# Look for the TensorFlow library next to the binaries
if(APPLE)
  set(CMAKE_INSTALL_RPATH "@loader_path")
elseif(UNIX)
  set(CMAKE_INSTALL_RPATH "$ORIGIN")
endif()

# Essentia lists the FFmpeg libraries in an order that doesn't work for static
# linking with GNU ld, list them again in dependency order
set(STATIC_FFMPEG_LIBRARIES "")
if(AVCODEC_LIBRARIES MATCHES "\\.a$")
  find_package(Threads REQUIRED)
  set(STATIC_FFMPEG_LIBRARIES ${AVFORMAT_LIBRARIES} ${AVCODEC_LIBRARIES} ${SWRESAMPLE_LIBRARIES} ${AVUTIL_LIBRARIES} Threads::Threads)
  if(UNIX)
    list(APPEND STATIC_FFMPEG_LIBRARIES m)
  endif()
endif()

set(ABNG_TOOLS abng_key abng_musicnn abng_rhythm)

foreach(tool ${ABNG_TOOLS})
  add_executable(${tool} ${tool}.cpp)
  target_compile_features(${tool} PRIVATE cxx_std_17)
  target_link_libraries(${tool} PRIVATE essentia ${STATIC_FFMPEG_LIBRARIES})
endforeach()

install(TARGETS ${ABNG_TOOLS} RUNTIME DESTINATION . COMPONENT tools)
