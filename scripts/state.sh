#!/usr/bin/env bash
set -euo pipefail

operation=$1
project=$(realpath "$2")
directory=$(realpath -m "$3")
branch=refs/heads/clipper-state
git_project() { git -C "$project" "$@"; }

fetch_state() {
  local status
  if git_project ls-remote --exit-code --heads origin "$branch" >/dev/null; then
    git_project fetch --quiet --depth=1 origin "$branch"
    git_project rev-parse FETCH_HEAD
  else
    status=$?
    if [[ $status != 2 ]]; then
      return "$status"
    fi
  fi
}

mkdir -p "$directory"
tip=$(fetch_state)
if [[ "$operation" == load ]]; then
  if [[ -n "$tip" ]]; then
    for file in catalog.json ledger.json; do
      if git_project cat-file -e "$tip:$file" 2>/dev/null; then
        git_project show "$tip:$file" > "$directory/$file"
      fi
    done
  fi
  exit 0
fi

case "$operation" in
  save-catalog) input=catalog.json; entry=catalog.json ;;
  save-ledger) input=next-ledger.json; entry=ledger.json ;;
  *) echo "Unknown state operation"; exit 1 ;;
esac

if [[ ! -s "$directory/$input" ]]; then
  exit 0
fi

index=$(mktemp -d)
trap 'rm -rf "$index"' EXIT
export GIT_INDEX_FILE="$index/index"
parents=()
if [[ -n "$tip" ]]; then
  git_project read-tree "$tip"
  parents=(-p "$tip")
else
  git_project read-tree --empty
fi
blob=$(git_project hash-object -w "$directory/$input")
git_project update-index --add --cacheinfo "100644,$blob,$entry"
tree=$(git_project write-tree)
message="Refresh GTA VI $entry"
if [[ "$operation" == save-catalog && ${GITHUB_RUN_ID:-} =~ ^[1-9][0-9]*$ && ${GITHUB_RUN_ATTEMPT:-} =~ ^[1-9][0-9]*$ ]] &&
   jq -e '((.moments | length) + (.sequences | length)) > 0' "$directory/$input" >/dev/null; then
  message="Publish GTA VI catalog for Shorts run $GITHUB_RUN_ID attempt $GITHUB_RUN_ATTEMPT"
elif [[ -n "$tip" && "$tree" == "$(git_project rev-parse "$tip^{tree}")" ]]; then
  exit 0
fi
commit=$(git_project -c user.name=github-actions -c user.email=41898282+github-actions[bot]@users.noreply.github.com \
  commit-tree "$tree" "${parents[@]}" -m "$message")
git_project push origin "$commit:$branch"
