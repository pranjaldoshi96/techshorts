#!/usr/bin/env bash
# Builds a TechShorts edition and publishes it to GitHub Pages (the repo's `edition` branch).
# Cron runs this every 2 hours; the app falls back to fetching feeds itself if an edition is over 8 hours old.
# See README.md for setup.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
state="${TECHSHORTS_STATE:-$HOME/.techshorts}"   # compiled publisher, editorial.json, site checkout
image="${SWIFT_IMAGE:-swift:6.2-jammy}"
deploy_key="${DEPLOY_KEY:-$HOME/.ssh/techshorts_deploy}"
remote="${EDITION_REMOTE:-git@github.com:pranjaldoshi96/TechShorts.git}"

mkdir -p "$state/bin" "$state/site"
exec 9>"$state/lock"
flock -n 9 || { echo "Another publish is running."; exit 0; }
echo "== $(date -u +%FT%TZ)"

sources=(
  ios/TechShorts/Models/Article.swift ios/TechShorts/Models/Edition.swift ios/TechShorts/Models/FeedSource.swift
  ios/TechShorts/Services/HTMLText.swift ios/TechShorts/Services/FeedParser.swift
  ios/TechShorts/Services/FeedService.swift ios/TechShorts/Services/Ranker.swift
  ios/TechShorts/Services/PageMetadata.swift ios/TechShorts/Services/Disk.swift
  server/main.swift
)
docker_run=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp)

# Rebuild only when the Swift sources change.
hash="$(cd "$repo" && cat "${sources[@]}" | sha256sum | cut -c1-16)-$image"
if [[ "$(cat "$state/bin/hash" 2>/dev/null)" != "$hash" ]]; then
  echo "Building publisher with $image"
  "${docker_run[@]}" -v "$repo":/src:ro -v "$state/bin":/out -w /src "$image" \
    swiftc -O -swift-version 5 -module-cache-path /tmp/mc -o /out/publisher "${sources[@]}"
  echo "$hash" > "$state/bin/hash"
fi

"${docker_run[@]}" -v "$state":/state "$image" /state/bin/publisher /state/editorial.json /state/site/edition.json

if [[ ! -f "$deploy_key" ]]; then
  echo "Built $state/site/edition.json; not published because $deploy_key is missing."
  exit 0
fi

# The edition branch holds a single commit, replaced on every publish, so the repo doesn't grow.
cd "$state/site"
touch .nojekyll
rm -rf .git
git init -q -b edition
git add -A
git -c user.name="TechShorts publisher" -c user.email="techshorts-publisher@users.noreply.github.com" \
  commit -q -m "Edition $(date -u +%FT%TZ)"
GIT_SSH_COMMAND="ssh -i $deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git push -q -f "$remote" edition
echo "Published to $remote (edition branch)"
