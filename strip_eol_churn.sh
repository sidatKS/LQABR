#!/usr/bin/env bash
set -euo pipefail

MERGE=9b1c275                       # PR #8 merge commit on leadq-dev

git fetch origin

BASE=$(git rev-parse "${MERGE}^1")  # leadq-dev state just before the merge
echo ">> pre-merge leadq-dev = $BASE"

# 1) all paths that differ between pre-merge and current leadq-dev
git diff --name-only "$BASE" origin/leadq-dev | sort > /tmp/all_changed.txt

# 2) paths with a REAL change (ignore whitespace + CRLF) -> these we KEEP
git diff --name-only -w --ignore-cr-at-eol "$BASE" origin/leadq-dev | sort > /tmp/keep_real.txt

# 3) churn = everything else, minus the always-keep surfaces (belt & suspenders)
comm -23 /tmp/all_changed.txt /tmp/keep_real.txt \
  | grep -vE '^(agents/text_voice/|packages/lqabr_core/)' \
  > /tmp/to_restore.txt || true

echo
echo "========================================================================"
echo "KEEP (real changes, untouched)  -> $(wc -l < /tmp/keep_real.txt) files"
echo "========================================================================"
cat /tmp/keep_real.txt
echo
echo "========================================================================"
echo "RESTORE (line-ending-only churn) -> $(wc -l < /tmp/to_restore.txt) files"
echo "========================================================================"
cat /tmp/to_restore.txt
echo

read -r -p "Restore the files in the second list to their pre-merge content? Type 'yes' to proceed: " ans
[ "$ans" = "yes" ] || { echo "Aborted. Nothing changed."; exit 0; }

git switch -c fix/strip-eol-churn origin/leadq-dev
if [ -s /tmp/to_restore.txt ]; then
  tr '\n' '\0' < /tmp/to_restore.txt | xargs -0 --no-run-if-empty git checkout "$BASE" --
fi

git status
git commit -m "Strip CRLF/LF churn from non-text_voice files (PR #8 fallout); keep real changes"
git push -u origin fix/strip-eol-churn
