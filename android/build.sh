#!/bin/bash
# Builds the debug APK:  android/build.sh        (any Gradle arguments replace the default task)
# Needs a JDK 17 to 21 and the Android SDK (platform 35). On a Mac with Android Studio both are found by themselves.
set -e
cd "$(dirname "$0")"
if [ -z "$JAVA_HOME" ] || ! "$JAVA_HOME/bin/java" -version 2>&1 | grep -Eq 'version "(17|21)'; then
    for candidate in "/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
                     "$(/usr/libexec/java_home -v 21 2>/dev/null)" "$(/usr/libexec/java_home -v 17 2>/dev/null)"; do
        if [ -n "$candidate" ] && "$candidate/bin/java" -version 2>&1 | grep -Eq 'version "(17|21)'; then
            export JAVA_HOME="$candidate"; break
        fi
    done
fi
if [ ! -f local.properties ] && [ -z "$ANDROID_HOME" ] && [ -z "$ANDROID_SDK_ROOT" ]; then
    for sdk in "$HOME/Library/Android/sdk" "$HOME/Android/Sdk"; do
        if [ -d "$sdk" ]; then echo "sdk.dir=$sdk" > local.properties; break; fi
    done
fi
if [ $# -eq 0 ]; then set -- assembleDebug; fi
./gradlew "$@"
if [ -f app/build/outputs/apk/debug/app-debug.apk ]; then echo "APK: $(pwd)/app/build/outputs/apk/debug/app-debug.apk"; fi
