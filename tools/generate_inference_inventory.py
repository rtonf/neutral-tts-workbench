"""Record exact separately acquired wheel contents without importing packages."""
from __future__ import annotations

import argparse
import email.parser
import email.policy
import hashlib
import json
import zipfile
from pathlib import Path


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).absolute().parents[1]
    locks = {part: json.loads((root / "config" / ("inference-" + part + ".lock.json")).read_text(encoding="utf-8"))
             for part in ("wheels", "sources", "assets", "notices")}
    artifacts = list(locks["wheels"]["wheels"])
    for recipe in locks["sources"]["recipes"]:
        artifacts.append({"name": recipe["distribution"], "version": recipe["version"],
                          "filename": recipe["wheel_file"], "sha256": recipe["wheel_sha256"],
                          "source_archive_url": recipe["url"], "source_archive_sha256": recipe["sha256"]})
    entries = []
    for record in sorted(artifacts, key=lambda item: item["name"]):
        path = args.wheelhouse / record["filename"]
        if path.name != record["filename"] or digest_file(path) != record["sha256"]:
            raise ValueError("Wheel content differs from the fixed catalog.")
        licenses, native = [], []
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate wheel member.")
            meta_names = [name for name in names if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
            if len(meta_names) != 1:
                raise ValueError("Wheel metadata identity is ambiguous.")
            metadata_bytes = archive.read(meta_names[0])
            metadata = email.parser.BytesParser(policy=email.policy.default).parsebytes(metadata_bytes)
            if metadata["Version"] != record["version"]:
                raise ValueError("Wheel version differs from the fixed catalog.")
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                if member.is_dir():
                    continue
                relative = member.filename
                filename = relative.rsplit("/", 1)[-1].lower()
                notice = any(word in filename for word in ("license", "licence", "copying", "notice", "eula"))
                binary = Path(filename).suffix in {".pyd", ".dll", ".so", ".dylib"}
                if notice or binary:
                    with archive.open(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    entry = {"member": relative, "size": member.file_size, "sha256": digest}
                    (licenses if notice else native).append(entry)
            external = [r for r in locks["notices"]["notices"]
                        if r["id"].replace("_", "-").lower() == record["name"].replace("_", "-").lower()]
            if not licenses and not external:
                raise ValueError("Wheel lacks a retained or separately pinned license notice.")
            declared = metadata.get("License-Expression") or metadata.get("License")
            # Large legacy metadata values duplicate full license text. Refer to
            # the retained text and exact METADATA bytes instead of re-encoding
            # arbitrary formatting escapes into this inventory's status field.
            if declared and (len(declared) > 160 or any(c in declared for c in (chr(10), chr(13), chr(92)))):
                declared = "SEE_RETAINED_LICENSE_TEXT"
            entries.append({**record, "license_declared": declared,
                            "metadata_member": meta_names[0], "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                            "license_members": licenses, "additional_notices": external,
                            "native_members": native, "bundled_in_application_distribution": False})
    result = {"schema_version": "neutral-inference-inventory-v1",
              "scope": "SEPARATELY_ACQUIRED_FIXED_ARTIFACTS",
              "license_interpretation": "See component conditions and original license texts; metadata is not permission.",
              "python": "3.13", "platform": "win_amd64", "wheels": entries,
              "assets": locks["assets"]["assets"],
              "external_prerequisites": ["Windows", "Python and Tcl/Tk", "GPU driver"],
              "runtime_redistribution": "OUTSIDE_CURRENT_DISTRIBUTION"}
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"wheels": len(entries), "native_files": sum(len(r["native_members"]) for r in entries),
                      "license_files": sum(len(r["license_members"]) for r in entries), "sha256": digest_file(args.output)}))


if __name__ == "__main__":
    main()
