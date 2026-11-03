#!/usr/bin/env bash
# Archives a Release build and uploads it to App Store Connect (it then shows up in TestFlight).
# Needs: Xcode signed in to your Apple Developer account (Xcode > Settings > Accounts).
# Usage: scripts/archive.sh  (TEAM_ID and BUNDLE_ID default to the project's; override to use others)
set -euo pipefail
cd "$(dirname "$0")/.."
TEAM_ID="${TEAM_ID:-A4HLS6BV3C}"
BUNDLE_ID="${BUNDLE_ID:-com.pranjald.techshorts}"

build_number="$(date +%Y%m%d.%H%M)"   # always increasing, as App Store Connect requires
archive="build/TechShorts.xcarchive"

xcodebuild -project TechShorts.xcodeproj -scheme TechShorts -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$archive" \
  DEVELOPMENT_TEAM="$TEAM_ID" PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" CURRENT_PROJECT_VERSION="$build_number" \
  -allowProvisioningUpdates archive

sed "s/TEAM_ID_PLACEHOLDER/$TEAM_ID/" scripts/ExportOptions.plist > build/ExportOptions.plist
xcodebuild -exportArchive -archivePath "$archive" -exportOptionsPlist build/ExportOptions.plist \
  -exportPath build/export -allowProvisioningUpdates

echo "Uploaded build $build_number. It appears in App Store Connect > TestFlight after processing (~10-30 min)."
