"""Deterministic zip builder, used by lambda.tf via a `data "external"` block
in place of `archive_file`.

archive_file hashes the zip bytes it produces, and a zip entry's timestamp
comes from the source file's mtime -- which differs between a local checkout
and a CI checkout of the same commit, so source_code_hash never stabilised
even when the lambda source was unchanged (every plan showed all three
functions as "will be updated"). This fixes every entry's timestamp and
permission bits instead, so the zip -- and its hash -- is byte-identical
whenever the source directory's *content* is unchanged.

External data source protocol: reads {"source_dir": ..., "output_path": ...}
as JSON on stdin, writes the zip, returns {"sha256_base64": ...} as JSON on
stdout (base64, to match what the AWS provider's source_code_hash and
Lambda's own CodeSha256 both use).
"""

import base64
import hashlib
import json
import os
import sys
import zipfile

FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


SKIP_DIRS = {"__pycache__"}
SKIP_FILE_SUFFIXES = (".pyc", ".pyo")
SKIP_FILE_NAMES = {".DS_Store"}


def build(source_dir, output_path):
    entries = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name in SKIP_FILE_NAMES or name.endswith(SKIP_FILE_SUFFIXES):
                continue
            full = os.path.join(root, name)
            entries.append((full, os.path.relpath(full, source_dir)))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arcname in entries:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if os.access(full, os.X_OK) else 0o644
            info.external_attr = mode << 16
            with open(full, "rb") as f:
                zf.writestr(info, f.read())

    with open(output_path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    return base64.b64encode(digest).decode()


def main():
    query = json.load(sys.stdin)
    sha256_base64 = build(query["source_dir"], query["output_path"])
    json.dump({"sha256_base64": sha256_base64, "output_path": query["output_path"]}, sys.stdout)


if __name__ == "__main__":
    main()
