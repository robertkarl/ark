#!/usr/bin/env bash
# deacon: file a SEV-2 tcorp ticket in a repo's `.tcorp/tickets/`.
#
# Usage:
#   file-ticket.sh <repo_root> <cti> <title> <bodyfile>
#
#   repo_root  the repo whose .tcorp/ should receive the ticket
#   cti        company/team/feature string, e.g. tcorp/ark-deacon/test-cheating
#   title      one-line ticket title
#   bodyfile   path to a file holding the markdown body (the evidence)
#
# Allocates the next ID as <PREFIX>-<max+1>, matching tcorp's TICKET-FORMAT.md.
# PREFIX comes from .tcorp/config.yaml (`prefix:`) if present, else is derived
# from the repo dir name (uppercased, non-alnum stripped, truncated to 6).
# `created`/`updated` are stamped with the current UTC time.
#
# Prints the created ticket path on success.

set -euo pipefail

repo="${1:?repo_root required}"
cti="${2:?cti required}"
title="${3:?title required}"
bodyfile="${4:?bodyfile required}"

[ -d "$repo" ] || { echo "deacon: repo not found: $repo" >&2; exit 1; }
[ -f "$bodyfile" ] || { echo "deacon: bodyfile not found: $bodyfile" >&2; exit 1; }

tickets_dir="$repo/.tcorp/tickets"
mkdir -p "$tickets_dir"

# Resolve prefix: config.yaml, else derived from dir name.
prefix=""
cfg="$repo/.tcorp/config.yaml"
if [ -f "$cfg" ]; then
  prefix=$(grep -E '^[[:space:]]*prefix:' "$cfg" | head -1 | sed -E 's/^[[:space:]]*prefix:[[:space:]]*//; s/[[:space:]]*$//; s/^["'"'"']//; s/["'"'"']$//' || true)
fi
if [ -z "$prefix" ]; then
  base=$(basename "$repo")
  prefix=$(printf '%s' "$base" | tr '[:lower:]' '[:upper:]' | tr -cd '[:alnum:]' | cut -c1-6)
fi
[ -n "$prefix" ] || prefix="TICKET"

# Next number = max existing for this prefix + 1.
maxn=0
shopt -s nullglob
for f in "$tickets_dir/${prefix}-"*.md; do
  n=$(basename "$f" .md | sed -E "s/^${prefix}-//")
  case "$n" in
    ''|*[!0-9]*) : ;;                     # skip non-numeric
    *) [ "$n" -gt "$maxn" ] && maxn="$n" ;;
  esac
done
shopt -u nullglob
id="${prefix}-$((maxn + 1))"
out="$tickets_dir/${id}.md"

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

{
  printf -- '---\n'
  printf 'id: %s\n' "$id"
  printf 'title: %s\n' "$title"
  printf 'cti: %s\n' "$cti"
  printf 'severity: 2\n'
  printf 'status: open\n'
  printf 'created: %s\n' "$now"
  printf 'updated: %s\n' "$now"
  printf 'reporter: deacon\n'
  printf -- '---\n\n'
  cat "$bodyfile"
} > "$out"

echo "$out"
