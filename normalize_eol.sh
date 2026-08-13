#!/usr/bin/env bash
set -euo pipefail

git fetch origin
git switch -c fix/normalize-eol origin/leadq-dev

# 1) consistent line endings for everyone, forever
cat > .gitattributes <<'GA'
# Normalize all text files to LF in the repo and on checkout
* text=auto eol=lf
*.sh  text eol=lf
*.bat text eol=crlf
*.cmd text eol=crlf
# Never touch binaries
*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.ico binary
*.pdf binary
*.xlsx binary
GA
git add .gitattributes

# 2) re-apply EOL rules to every tracked file (content unchanged, only endings)
git add --renormalize .

# 3) preview
echo "==== files whose endings will be normalized to LF ===="
git diff --cached --name-only
echo "Total: $(git diff --cached --name-only | wc -l) files (+ .gitattributes)"
read -r -p "Commit + push this normalization? Type 'yes': " ans
[ "$ans" = "yes" ] || { echo "Aborted. To undo: git switch leadq-dev && git branch -D fix/normalize-eol"; exit 0; }

git commit -m "Normalize line endings to LF; add .gitattributes (fixes CRLF churn from PR #8)"
git push -u origin fix/normalize-eol
echo ">> Pushed fix/normalize-eol — open a PR into leadq-dev for svktekninjas."
