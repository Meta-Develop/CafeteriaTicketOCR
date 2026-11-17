"""Reuse extracted Cargo sources without downloads or changes to shared caches."""
import json
import os
from pathlib import Path
import sys
import tomllib

destination = Path(sys.argv[1])
cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
registries = list((cargo_home / "registry/src").glob("index.crates.io-*"))
if len(registries) != 1:
    raise SystemExit("One existing crates.io source cache is required for offline source reuse.")
sources = registries[0]
index = cargo_home / "registry/index" / sources.name / ".cache"
destination.mkdir(exist_ok=True)
count = 0
for source in sources.iterdir():
    manifest = source / "Cargo.toml"
    if not manifest.is_file():
        continue
    package = tomllib.loads(manifest.read_text())["package"]
    name, version = package["name"], package["version"]
    if len(name) == 1:
        relative = "1/" + name
    elif len(name) == 2:
        relative = "2/" + name
    elif len(name) == 3:
        relative = "3/" + name[0] + "/" + name
    else:
        relative = name[:2] + "/" + name[2:4] + "/" + name
    metadata = index / relative
    if not metadata.is_file():
        continue
    checksum = None
    for record in metadata.read_bytes().split(b"\0"):
        if record.startswith(b"{"):
            entry = json.loads(record)
            if entry["vers"] == version:
                checksum = entry["cksum"]
                break
    if checksum is None:
        continue
    target = destination / source.name
    target.mkdir(exist_ok=True)
    for entry in source.iterdir():
        link = target / entry.name
        if entry.name != ".cargo-checksum.json" and not link.exists():
            link.symlink_to(entry, target_is_directory=entry.is_dir())
    # Sources are existing local inputs. The registry checksum preserves a
    # portable lockfile; no download or registry-source integrity claim is made.
    (target / ".cargo-checksum.json").write_text(json.dumps({"files": {}, "package": checksum}))
    count += 1
if not count:
    raise SystemExit("No reusable cached crate sources were found.")
