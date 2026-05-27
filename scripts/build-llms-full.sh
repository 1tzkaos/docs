#!/usr/bin/env bash
# Generate llms-full.txt by concatenating the body of every page listed in
# docs.json's nav (after frontmatter strip), separated by --- delimiters.
#
# Usage:  bash scripts/build-llms-full.sh > llms-full.txt
# CI:     scripts/build-llms-full.sh > /tmp/new.txt && diff /tmp/new.txt llms-full.txt

set -euo pipefail
cd "$(dirname "$0")/.."

# Header lifted verbatim from llms.txt (everything up to the first blank line)
sed -n '1,/^$/p' llms.txt
echo ""

# Walk MDX pages in docs.json nav order
python3 << 'PY'
import json, pathlib, re, sys

nav = json.loads(pathlib.Path("docs.json").read_text())
pages = []

def walk(g):
    for item in g.get("groups", []):
        for p in item.get("pages", []):
            if isinstance(p, str) and not p.startswith(("GET ", "POST ", "PUT ", "DELETE ")):
                pages.append(p)
            elif isinstance(p, dict):
                walk(p)

for tab in nav["navigation"]["tabs"]:
    walk(tab)

# Deduplicate while preserving order
seen = set()
for page in pages:
    if page in seen:
        continue
    seen.add(page)
    md = pathlib.Path(f"{page}.mdx")
    if not md.exists():
        continue
    body = md.read_text()
    # Strip YAML frontmatter
    body = re.sub(r'^---\n.*?\n---\n', '', body, count=1, flags=re.DOTALL)
    sys.stdout.write(f"\n\n---\n\n# {page}\n\n")
    sys.stdout.write(body)
PY
