"""Prints a snapshot's Cloudflare export as JSON: its EXPORT_MANIFEST.json plus
the list of .tf files in the tree.

Runs inside cf-backup, fed on stdin, so nothing has to be mounted:

    docker compose exec -T cf-backup python - <snapshot id> < manifest.py

It opens the snapshot the way `cloudflare diff` and `cloudflare apply` do
(sha256 check, extraction of the cloudflare component).
"""

import json
import sys

# The engine CLI first: it loads the cloudflare command plugin, which imports
# backuphelper_cloudflare.snapshot - importing that module first would make
# the plugin's import circular and the engine log that it failed to load.
import backuphelper.cli  # noqa: F401
from backuphelper_cloudflare.export import EXPORT_MANIFEST_NAME
from backuphelper_cloudflare.snapshot import open_export

with open_export(sys.argv[1]) as tree:
    manifest = json.loads((tree / EXPORT_MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["files"] = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*.tf"))
print(json.dumps(manifest))
