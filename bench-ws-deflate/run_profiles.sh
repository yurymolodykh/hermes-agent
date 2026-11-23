#!/bin/bash
# py-spy sample share per deflate variant, two representative loop shapes.
# Output: one JSON line per (variant, mode) -> profile-shares.jsonl
set -u
cd "$(dirname "$0")"
OUT=profile-shares.jsonl
: > "$OUT"
for mode in delta-coalesced panel; do
  for v in on level1 level4 memlevel1 notakeover off; do
    case "$v" in
      on) extra="--deflate on" ;;
      off) extra="--deflate off" ;;
      level1) extra="--deflate on --level 1" ;;
      level4) extra="--deflate on --level 4" ;;
      memlevel1) extra="--deflate on --memlevel 1" ;;
      notakeover) extra="--deflate on --no-context-takeover" ;;
    esac
    echo "### $v / $mode" >&2
    python3 profile_share.py --variant "$v" --extra "$extra" --mode "$mode" \
      --seconds 15 --clients 8 2>/dev/null >> "$OUT"
  done
done
echo "wrote $OUT"
