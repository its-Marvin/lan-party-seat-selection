#!/usr/bin/env bash
# Point every alias tag of the image at the newest published version it covers:
#   :X and :X.Y  the highest stable release in that line, for every line released so far
#   :latest      the release GitHub marks as "Latest"
#
# Usage: update-image-aliases.sh IMAGE   (needs gh, jq, docker buildx)
#
# Runs one at a time (the workflow's docker-aliases concurrency group). GitHub keeps only
# one pending job per group and cancels older pending ones, so a run can't rely on the
# run for its own release happening: each run reconciles *all* aliases from the full
# list of releases. Whichever run comes last therefore leaves every alias right, and a
# fix for an older line (v1.1.5 after v1.2.0) can't move an alias backwards.
set -euo pipefail

image="$1"

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
    echo ":$alias - image $target isn't published yet; a later run sets it"
  else
    docker buildx imagetools create --tag "$image:$alias" "$image:$target"
    echo ":$alias -> $target"
  fi
}

# every major line (X) and minor line (X.Y) that has a stable release
for line in $(cut -d. -f1 <<< "$stable" | sort -un) $(cut -d. -f1,2 <<< "$stable" | sort -uV); do
  point "$line" "$(newest_in "$line")"
done

latest=""
if latest_tag=$(gh api "repos/$GITHUB_REPOSITORY/releases/latest" --jq .tag_name 2> /dev/null); then
  latest="${latest_tag#v}"
fi
point latest "$latest"
