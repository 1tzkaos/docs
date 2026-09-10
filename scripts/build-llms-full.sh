#!/usr/bin/env bash
# Generate llms-full.txt by concatenating the body of every page listed in
# docs.json's nav (after frontmatter strip), separated by --- delimiters.
#
# Usage:  bash scripts/build-llms-full.sh > llms-full.txt
# CI:     scripts/build-llms-full.sh > /tmp/new.txt && diff /tmp/new.txt llms-full.txt

set -euo pipefail
cd "$(dirname "$0")/.."

# The coverage page's gap disclosure is generated from the live index by
# scripts/gen-data-gaps.py, and its output (snippets/data-gaps.mdx) is
# committed. Regenerating it needs database reachability, so it is OPT-IN:
# without REGEN_DATA_GAPS=1 this build uses the committed snippet and stays
# deterministic, which is what lets CI diff its output against llms-full.txt.
if [[ "${REGEN_DATA_GAPS:-0}" == "1" ]]; then
  python3 scripts/gen-data-gaps.py >&2
fi

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
    # Inline /snippets imports so the flattened text keeps the shared content
    for name, snippet in re.findall(
        r"^import\s+(\w+)\s+from\s+['\"](/snippets/[^'\"]+)['\"];?\s*$",
        body, flags=re.MULTILINE):
        sp = pathlib.Path(snippet.lstrip("/"))
        if not sp.exists():
            sys.exit(f"missing snippet {snippet} imported by {page}")
        text = re.sub(r'^---\n.*?\n---\n', '', sp.read_text(), count=1, flags=re.DOTALL)
        body = re.sub(rf"^<{name}\s*/>\s*$", lambda _m: text.rstrip("\n"),
                      body, flags=re.MULTILINE)
        body = re.sub(rf"^import\s+{name}\s+from\s+['\"]{re.escape(snippet)}['\"];?\s*\n",
                      '', body, flags=re.MULTILINE)
    # MDX comments are authoring notes, not content — drop them so generated
    # snippets don't leak "do not edit by hand" banners into the LLM text.
    body = re.sub(r'^\{/\*.*?\*/\}\n', '', body, flags=re.DOTALL | re.MULTILINE)
    sys.stdout.write(f"\n\n---\n\n# {page}\n\n")
    sys.stdout.write(body)
PY
