#!/usr/bin/env bash
# Point the alias tags of the image at the newest published version they cover:
#   :X and :X.Y  the highest stable release in that line (for this release's line)
#   :latest      the release GitHub marks as "Latest"
#
# Usage: update-image-aliases.sh IMAGE RELEASE_TAG   (needs gh, jq, docker buildx)
#
# Runs one at a time (the workflow's docker-aliases concurrency group). Every alias
# is computed from the full list of releases, not from the release of this run, so
# whichever run comes last leaves them right: a fix for an older line (v1.1.5 after
# v1.2.0) or two releases published at once can't move an alias backwards.
set -euo pipefail

image="$1"
version="${2#v}"
major="${version%%.*}"
rest="${version#*.}"
minor="${rest%%.*}"

# stable (not draft, not pre-release) semver releases, oldest to newest version
stable=$(gh api "repos/$GITHUB_REPOSITORY/releases" --paginate \
  | jq -r '.[] | select((.draft or .prerelease) | not) | .tag_name' \
  | sed 's/^v//' | { grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true; } | sort -V)

newest_in() {   # newest stable version starting with "$1."
  { grep -E "^${1//./\\.}\." <<< "$stable" || true; } | tail -n 1
}

point() {       # point :$1 at the published image of version $2
  local alias="$1" target="$2"
  if [ -z "$target" ]; then
    echo ":$alias - no stable release yet, left unchanged"
  elif ! docker buildx imagetools inspect "$image:$target" > /dev/null 2>&1; then
    echo ":$alias - image $target isn't published yet; its own run sets it"
  else
    docker buildx imagetools create --tag "$image:$alias" "$image:$target"
    echo ":$alias -> $target"
  fi
}

point "$major" "$(newest_in "$major")"
point "$major.$minor" "$(newest_in "$major.$minor")"

latest=""
if latest_tag=$(gh api "repos/$GITHUB_REPOSITORY/releases/latest" --jq .tag_name 2> /dev/null); then
  latest="${latest_tag#v}"
fi
point latest "$latest"
