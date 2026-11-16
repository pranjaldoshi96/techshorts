#!/usr/bin/env bash
# Archives a Release build, exports a signed IPA, validates it with App Store Connect and, with --upload, uploads it
# (it then shows up in TestFlight).
#
# Usage: scripts/archive.sh [--upload]
#   TEAM_ID and BUNDLE_ID default to the project's; override to use others. BUNDLE_ID is the app's; the
#   notification extension's is derived from it (BUNDLE_ID.NotificationService).
#
# Two different credentials are involved, on purpose:
#   - Signing uses the Apple ID signed in to Xcode (Xcode > Settings > Accounts). The archive and the export are
#     run WITHOUT the App Store Connect API key: that key's role has no access to cloud-managed distribution
#     certificates, and an export given the key fails with "Cloud signing permission error".
#   - Validation and upload use the App Store Connect API key through altool:
#       ASC_KEY_ID, ASC_ISSUER_ID   required for validate and upload
#       ASC_KEY_PATH                optional path to AuthKey_<ASC_KEY_ID>.p8; without it altool looks in
#                                   ./private_keys, ~/private_keys, ~/.private_keys, ~/.appstoreconnect/private_keys
#     Without ASC_KEY_ID the script stops after the export and prints where the IPA is.
set -euo pipefail
cd "$(dirname "$0")/.."
TEAM_ID="${TEAM_ID:-A4HLS6BV3C}"
BUNDLE_ID="${BUNDLE_ID:-com.pranjald.techgist}"
upload=0
[ "${1:-}" = "--upload" ] && upload=1

# Always increasing, as App Store Connect requires: the date, then the time as HHMM. App Store Connect drops
# leading zeros after the dot (20261104.0039 is listed as 20261104.39), so they are dropped here too: what is
# printed is what App Store Connect shows.
build_number="$(date +%Y%m%d).$((10#$(date +%H%M)))"
archive="build/Techgist.xcarchive"
export_dir="build/export"

xcodebuild -project Techgist.xcodeproj -scheme Techgist -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$archive" \
  DEVELOPMENT_TEAM="$TEAM_ID" APP_BUNDLE_ID="$BUNDLE_ID" CURRENT_PROJECT_VERSION="$build_number" \
  -allowProvisioningUpdates archive

rm -rf "$export_dir"
sed "s/TEAM_ID_PLACEHOLDER/$TEAM_ID/" scripts/ExportOptions.plist > build/ExportOptions.plist
xcodebuild -exportArchive -archivePath "$archive" -exportOptionsPlist build/ExportOptions.plist \
  -exportPath "$export_dir" -allowProvisioningUpdates

ipa="$(ls "$export_dir"/*.ipa | head -1)"
echo "Exported build $build_number to $ipa"

if [ -z "${ASC_KEY_ID:-}" ] || [ -z "${ASC_ISSUER_ID:-}" ]; then
  echo "ASC_KEY_ID and ASC_ISSUER_ID are not set: not validated, not uploaded."
  exit 0
fi
auth=(--apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID")
[ -n "${ASC_KEY_PATH:-}" ] && auth+=(--p8-file-path "$ASC_KEY_PATH")

xcrun altool --validate-app -f "$ipa" -t ios "${auth[@]}"
echo "Build $build_number passed validation."

if [ "$upload" = 1 ]; then
  xcrun altool --upload-app -f "$ipa" -t ios "${auth[@]}"
  echo "Uploaded build $build_number. It appears in App Store Connect > TestFlight after processing (~10-30 min)."
else
  echo "Not uploaded. Run again with --upload, or: xcrun altool --upload-app -f $ipa -t ios --apiKey … --apiIssuer …"
fi
