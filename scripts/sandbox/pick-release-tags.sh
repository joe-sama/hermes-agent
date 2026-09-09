#!/usr/bin/env bash
# Pick the release tags the install/update E2E should update FROM.
#
# Emits a JSON array of tag names on stdout, suitable for a GitHub Actions
# matrix (`fromJSON`). Choosing at runtime rather than hardcoding keeps the
# matrix honest as releases land: a pinned list silently stops covering the
# newest release the day after it ships, and pins the "oldest" forever even
# after it stops being a version anyone still runs.
#
# Selection: the newest tag, the oldest tag, and evenly spaced tags in between.
# Newest catches "did the last release break updating?", oldest is the longest
# upgrade jump anyone can still make, and the spread samples the migrations in
# between (config-schema bumps, venv layout changes, dependency floors).
#
# Usage:
#   scripts/sandbox/pick-release-tags.sh [--count N] [--repo DIR] [--merged REF] [--remote URL]
#
#   --count   how many tags to emit (default 5, minimum 1). Fewer tags than
#             requested emits all of them.
#   --repo    repository to read tags from (default: this checkout).
#   --merged only sample releases contained in REF. A fork must not test a
#             newer upstream release as an upgrade to its older target tree.
#   --remote read canonical tag names/peeled commits with ls-remote. Requires
#             --merged and complete local target ancestry; fetches no objects.
#   --include-commits emit {tag, commit} records for a pinned CI matrix.
#
# Reads tags from the local checkout, so it needs one fetched with tags
# (actions/checkout with fetch-depth: 0, or `fetch-tags: true`). A shallow
# checkout has no tags and this exits non-zero rather than silently emitting an
# empty matrix.
#
# Only vYYYY.M.D[.N] release tags are considered; the repo also carries
# backup/* and one-off tags that are not releases.

set -euo pipefail

COUNT=5
# Default to the repository containing this script, resolved through its real
# path so a symlinked or copied script still reads the checkout it lives in
# rather than whatever repo the caller happens to be standing in.
REPO=""
MERGED=""
REMOTE=""
INCLUDE_COMMITS=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --count)
      [ "$#" -ge 2 ] || { echo 'error: --count needs a value' >&2; exit 1; }
      COUNT="$2"; shift 2 ;;
    --repo)
      [ "$#" -ge 2 ] || { echo 'error: --repo needs a value' >&2; exit 1; }
      REPO="$2"; shift 2 ;;
    --merged)
      [ "$#" -ge 2 ] || { echo 'error: --merged needs a ref' >&2; exit 1; }
      MERGED="$2"; shift 2 ;;
    --remote)
      [ "$#" -ge 2 ] || { echo 'error: --remote needs a URL' >&2; exit 1; }
      REMOTE="$2"; shift 2 ;;
    --include-commits) INCLUDE_COMMITS=true; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$COUNT" in
  ''|*[!0-9]*) echo "error: --count must be a positive integer: $COUNT" >&2; exit 1 ;;
esac
[ "$COUNT" -ge 1 ] || { echo 'error: --count must be at least 1' >&2; exit 1; }

# Resolve the script's own location through symlinks, then ask git which
# worktree that path belongs to. Deriving the repo from the script rather than
# from $PWD means a copied script cannot silently report a different checkout's
# tags, and --show-toplevel keeps it correct when invoked from a subdirectory.
if [ -z "$REPO" ]; then
  script_path="${BASH_SOURCE[0]}"
  if command -v readlink >/dev/null 2>&1; then
    script_path="$(readlink -f "$script_path" 2>/dev/null || printf '%s' "$script_path")"
  fi
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"
  REPO="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$script_dir")"
fi

# sort -V orders v2026.4.8 before v2026.4.13 (numeric), which a plain
# lexicographic sort gets wrong.
tag_filter=()
declare -A release_commits=()
if [ -n "$MERGED" ]; then
  # Resolve first so an invalid ref cannot become a successful empty matrix.
  merged_commit="$(git -C "$REPO" rev-parse --verify "${MERGED}^{commit}")"
  tag_filter=(--merged "$merged_commit")
fi
if [ -n "$REMOTE" ]; then
  [ -n "$MERGED" ] || { echo 'error: --remote requires --merged' >&2; exit 1; }
  # The fork checkout already contains every eligible ancestor. Importing ALL
  # upstream tag objects/history downloads unrelated future commits and hit
  # GitHub HTTP 429 in scheduled CI. Only read the ref advertisement instead.
  # Membership also avoids cat-file on unknown IDs, which a partial clone can
  # silently turn into an on-demand network fetch.
  ancestor_ids="$(git -C "$REPO" rev-list "$merged_commit")"
  remote_refs="$(git -C "$REPO" ls-remote --tags "$REMOTE" 'refs/tags/v*')"
  declare -A ancestors=()
  while read -r commit_id; do
    [ -z "$commit_id" ] || ancestors["$commit_id"]=1
  done <<< "$ancestor_ids"
  eligible=()
  while read -r commit_id ref_name; do
    if [[ "$ref_name" =~ ^refs/tags/(v[0-9]{4}\.[0-9]+\.[0-9]+(\.[0-9]+)?)(\^\{\})?$ ]]; then
      tag_name="${BASH_REMATCH[1]}"
      # Annotated tag objects are not commits; their ^{} advertisement is.
      if [[ -n "${ancestors[$commit_id]+present}" ]]; then
        eligible+=("$tag_name")
        release_commits["$tag_name"]="$commit_id"
      fi
    fi
  done <<< "$remote_refs"
  mapfile -t tags < <(printf '%s\n' "${eligible[@]}" | sed '/^$/d' | sort -Vu)
else
  mapfile -t tags < <(
    git -C "$REPO" tag --list 'v*' "${tag_filter[@]}" \
      | grep -E '^v[0-9]{4}\.[0-9]+\.[0-9]+(\.[0-9]+)?$' \
      | sort -V
  )
fi

total="${#tags[@]}"
if [ "$total" -eq 0 ]; then
  echo "error: no release tags found in $REPO" >&2
  echo '       A shallow clone has no tags: fetch with tags (actions/checkout' >&2
  echo '       with fetch-depth: 0, or fetch-tags: true).' >&2
  exit 1
fi

if [ "$total" -le "$COUNT" ]; then
  picked=("${tags[@]}")
elif [ "$COUNT" -eq 1 ]; then
  # One slot means the newest release; there is no span to spread across.
  picked=("${tags[$((total - 1))]}")
else
  # Evenly spaced indices across [0, total-1], endpoints included, so the
  # oldest and newest are always present and the rest are spread between them.
  picked=()
  for slot in $(seq 0 $((COUNT - 1))); do
    # Round to nearest rather than truncate, so the spacing does not bunch
    # toward the oldest end.
    index=$(( (slot * (total - 1) * 2 + (COUNT - 1)) / ((COUNT - 1) * 2) ))
    candidate="${tags[$index]}"
    # Guard against a duplicate if rounding lands twice on the same tag.
    case " ${picked[*]-} " in
      *" $candidate "*) continue ;;
    esac
    picked+=("$candidate")
  done
fi

printf '['
for i in "${!picked[@]}"; do
  [ "$i" -eq 0 ] || printf ','
  if [ "$INCLUDE_COMMITS" = true ]; then
    tag_name="${picked[$i]}"
    commit_id="${release_commits[$tag_name]:-}"
    if [ -z "$commit_id" ]; then
      commit_id="$(git -C "$REPO" rev-parse "${tag_name}^{commit}")"
    fi
    printf '{"tag":"%s","commit":"%s"}' "$tag_name" "$commit_id"
  else
    printf '"%s"' "${picked[$i]}"
  fi
done
printf ']\n'
