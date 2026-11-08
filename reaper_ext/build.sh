#!/bin/sh
# Собирает универсальный reaper_pt_session_import.dylib (arm64 + x86_64)
set -e
cd "$(dirname "$0")"
mkdir -p out
clang -fobjc-arc -dynamiclib -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 -framework Cocoa \
  -DPX_VERSION="\"${APP_VERSION:-dev}\"" -o out/reaper_pt_session_import.dylib reaper_pt_session_import.m
codesign --force --sign - out/reaper_pt_session_import.dylib
echo built out/reaper_pt_session_import.dylib
