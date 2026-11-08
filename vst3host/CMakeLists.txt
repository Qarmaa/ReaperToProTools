cmake_minimum_required(VERSION 3.20)
project(vst3host CXX OBJCXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_OSX_ARCHITECTURES arm64)
set(SDK_ROOT "${VST3_SDK_ROOT}")
add_executable(vst3host main.cpp
  ${SDK_ROOT}/base/source/baseiids.cpp ${SDK_ROOT}/base/source/fobject.cpp ${SDK_ROOT}/base/source/fstring.cpp
  ${SDK_ROOT}/base/source/fbuffer.cpp ${SDK_ROOT}/base/source/fdebug.cpp ${SDK_ROOT}/base/source/updatehandler.cpp
  ${SDK_ROOT}/base/source/fdynlib.cpp ${SDK_ROOT}/base/source/timer.cpp ${SDK_ROOT}/base/source/fstreamer.cpp
  ${SDK_ROOT}/pluginterfaces/base/conststringtable.cpp ${SDK_ROOT}/pluginterfaces/base/coreiids.cpp
  ${SDK_ROOT}/pluginterfaces/base/funknown.cpp ${SDK_ROOT}/pluginterfaces/base/ustring.cpp
  ${SDK_ROOT}/public.sdk/source/common/memorystream.cpp ${SDK_ROOT}/public.sdk/source/common/commoniids.cpp
  ${SDK_ROOT}/public.sdk/source/vst/vstinitiids.cpp
  ${SDK_ROOT}/public.sdk/source/vst/hosting/module.cpp ${SDK_ROOT}/public.sdk/source/vst/hosting/module_mac.mm
  ${SDK_ROOT}/public.sdk/source/vst/hosting/hostclasses.cpp ${SDK_ROOT}/public.sdk/source/vst/hosting/pluginterfacesupport.cpp
  ${SDK_ROOT}/public.sdk/source/vst/hosting/parameterchanges.cpp ${SDK_ROOT}/public.sdk/source/vst/hosting/processdata.cpp
  ${SDK_ROOT}/public.sdk/source/vst/hosting/eventlist.cpp ${SDK_ROOT}/public.sdk/source/vst/hosting/connectionproxy.cpp
  ${SDK_ROOT}/public.sdk/source/vst/utility/stringconvert.cpp ${SDK_ROOT}/public.sdk/source/common/commonstringconvert.cpp
  ${SDK_ROOT}/base/thread/source/flock.cpp ${SDK_ROOT}/public.sdk/source/common/threadchecker_mac.mm
  ${SDK_ROOT}/public.sdk/source/vst/moduleinfo/moduleinfoparser.cpp)
target_include_directories(vst3host PRIVATE ${SDK_ROOT})
target_compile_definitions(vst3host PRIVATE RELEASE=1)
target_link_libraries(vst3host PRIVATE "-framework Cocoa" "-framework CoreFoundation")
set_source_files_properties("${VST3_SDK_ROOT}/public.sdk/source/vst/hosting/module_mac.mm" PROPERTIES COMPILE_FLAGS "-fobjc-arc")

set_source_files_properties("${VST3_SDK_ROOT}/public.sdk/source/common/threadchecker_mac.mm" PROPERTIES COMPILE_FLAGS "-fobjc-arc")
