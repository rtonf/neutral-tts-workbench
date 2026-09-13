"""Reproducible source-only wheels, without running upstream build scripts.

A reviewed recipe supplies exact archive bytes, package roots, data-file types
and dependency metadata. Code bytes are preserved; the packaging metadata is
explicitly identified as a local integration build, with retained license text.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import PurePosixPath


def source_wheel(data, recipe):
    if len(data) != recipe["size"] or len(data) > 20 * 1024**2 or hashlib.sha256(data).hexdigest() != recipe["sha256"]:
        raise ValueError("Source archive differs from the fixed recipe.")
    members = {}
    def retain(name, payload):
        path = PurePosixPath(name)
        if path.is_absolute() or "\\" in name or ":" in name or any(v in {"", ".", ".."} for v in name.split("/")):
            raise ValueError("Source archive path is invalid.")
        relative = "/".join(path.parts[1:])
        if not relative or relative in members or len(payload) > 2 * 1024**2:
            raise ValueError("Source archive member is oversized or ambiguous.")
        members[relative] = payload
        if len(members) > 4096 or sum(map(len, members.values())) > 30 * 1024**2:
            raise ValueError("Source archive exceeds the extraction bound.")
    if recipe["format"] == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 8192 or sum(i.file_size for i in archive.infolist()) > 30 * 1024**2:
                raise ValueError("Source archive is oversized.")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Source archive links/encryption are unsupported.")
                retain(info.orig_filename, archive.read(info))
    elif recipe["format"] == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for index, info in enumerate(archive):
                if index >= 8192:
                    raise ValueError("Source archive has too many members.")
                if info.isdir():
                    continue
                if not info.isfile() or info.size > 2 * 1024**2:
                    raise ValueError("Source archive links and special files are unsupported.")
                retain(info.name, archive.extractfile(info).read())
    else:
        raise ValueError("Source archive format is unsupported.")
    project, version = recipe["distribution"], recipe["version"]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", project) or not re.fullmatch(r"[0-9][a-z0-9.+]*", version):
        raise ValueError("Wheel identity is invalid.")
    files = {name: value for name, value in members.items()
             if any(name.startswith(package + "/") for package in recipe["packages"])
             and (name.endswith(".py") or PurePosixPath(name).suffix in recipe.get("data_suffixes", []))}
    for package in recipe["packages"]:
        if package + "/__init__.py" not in files:
            raise ValueError("The source archive does not contain the expected package.")
    info = project + "-" + version + ".dist-info"
    license_name = recipe["license_file"]
    if license_name not in members:
        raise ValueError("The reviewed license text is missing.")
    files[info + "/licenses/LICENSE"] = members[license_name]
    metadata = ["Metadata-Version: 2.4", "Name: " + project.replace("_", "-"), "Version: " + version,
                "Summary: Fixed-source local integration build", "Requires-Python: >=3.13,<3.14",
                "License-Expression: " + recipe["license"], "License-File: licenses/LICENSE"]
    for requirement in recipe["dependencies"]:
        if any(c in requirement for c in "\r\n"):
            raise ValueError("Dependency metadata contains a line break.")
        metadata.append("Requires-Dist: " + requirement)
    files[info + "/METADATA"] = ("\n".join(metadata) + "\n\n").encode()
    files[info + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: neutral-source-wheel-v1\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[info + "/integration-provenance.json"] = json.dumps({
        "source_url": recipe["url"], "source_sha256": recipe["sha256"],
        "source_revision": recipe["revision"], "packaging": "Modified integration metadata; package code bytes preserved.",
        "recipe_sha256": hashlib.sha256(json.dumps(
            {k: v for k, v in recipe.items() if k not in {"wheel_sha256", "wheel_file"}},
            sort_keys=True).encode()).hexdigest(),
    }, sort_keys=True, separators=(",", ":")).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, value in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(value)])
    writer.writerow([info + "/RECORD", "", ""])
    files[info + "/RECORD"] = record.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            archive.writestr(member, value)
    return project + "-" + version + "-py3-none-any.whl", output.getvalue()
