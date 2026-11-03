#!/usr/bin/env bash
# Builds the app's feed code as a macOS command-line tool and runs it against the live feeds.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${TMPDIR:-/tmp}/techshorts-feedcheck"
main="${TMPDIR:-/tmp}/main.swift"
cp scripts/feedcheck-main.swift "$main"
swiftc -O -o "$out" TechShorts/Models/Article.swift TechShorts/Models/FeedSource.swift TechShorts/Models/Edition.swift \
  TechShorts/Services/HTMLText.swift TechShorts/Services/FeedParser.swift TechShorts/Services/FeedService.swift \
  TechShorts/Services/Ranker.swift \
  TechShorts/Services/Disk.swift "$main"
"$out"
