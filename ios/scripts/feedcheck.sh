#!/usr/bin/env bash
# Builds the app's feed code as a macOS command-line tool and runs it against the live feeds.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${TMPDIR:-/tmp}/techshorts-feedcheck"
main="${TMPDIR:-/tmp}/main.swift"
cp scripts/feedcheck-main.swift "$main"
swiftc -O -o "$out" Techgist/Models/Article.swift Techgist/Models/FeedSource.swift Techgist/Models/Edition.swift \
  Techgist/Services/HTMLText.swift Techgist/Services/FeedParser.swift Techgist/Services/FeedService.swift \
  Techgist/Services/Ranker.swift \
  Techgist/Services/Disk.swift "$main"
"$out"
