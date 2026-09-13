"""Build a fixed-commit, first-party Python/Tk Windows installer candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "src"))
from neutral_tts.install_contract import create_test_layout, verify_layout
from generate_source_metadata import build_spdx_document, build_cyclonedx_document, _validate_output_parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--archive", type=Path, help="Optional fresh deterministic Windows ZIP path")
    args = parser.parse_args()
    repo = Path(__file__).absolute().parents[1]
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(repo), *arguments])
    commit = git("rev-parse", args.commit + "^{commit}").decode().strip()
    tree = git("rev-parse", commit + "^{tree}").decode().strip()
    tracked = git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
    selected = {name: ("source" if name.endswith((".py", ".pyw")) else "documentation") for name in tracked
                if (name.startswith("src/neutral_tts/") and name.endswith(".py"))
                or name in {"launch_desktop.pyw", "windows_setup.pyw", "LICENSE", "README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "config/tokenizer-windows-py313.lock.txt",
                            "config/inference-assets.lock.json", "config/inference-sources.lock.json", "config/inference-wheels.lock.json", "config/inference-notices.lock.json", "config/inference-inventory.json"}
                or (name.startswith("docs/") and name.endswith(".md"))}
    if not {"launch_desktop.pyw", "windows_setup.pyw", "src/neutral_tts/installer.py"} <= set(selected):
        raise ValueError("The selected commit does not contain the installer and launcher.")
    destination = args.destination.absolute()
    if not destination.parent.is_dir() or destination.exists():
        raise ValueError("Use a fresh destination under an existing staging parent.")
    with tempfile.TemporaryDirectory(prefix="neutral-source-build-", dir=destination.parent) as name:
        source = Path(name)
        for relative in selected:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git("show", commit + ":" + relative))
        lock = json.loads(git("show", commit + ":config/dependency-closure.lock.json"))
        allowlist = json.loads(git("show", commit + ":compliance/source-asset-allowlist.json"))
        source_paths = sorted(selected)
        for filename, builder in (("SBOM.spdx.json", build_spdx_document), ("SBOM.cdx.json", build_cyclonedx_document)):
            document = builder(repo, commit, lock, allowlist=allowlist, file_paths=source_paths)
            (source / filename).write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
            selected[filename] = "metadata"
        manifest = create_test_layout(source, destination, selected,
            candidate_id="candidate-" + commit[:12], source_commit=commit, source_tree=tree,
            app_root_name="neutral-tts-workbench", verification_scope="LOCAL_WINDOWS_SOURCE_CANDIDATE")
    verify_layout(destination)
    result = {"commit": commit, "tree": tree, "file_count": manifest["file_count"],
                      "files_sha256": manifest["files_sha256"], "signature": "unsigned",
                      "runtime_included": False, "fresh_windows_verified": False,
                      "sbom_scope": "selected source payload; generated SBOMs and ownership manifest excluded from their own file lists"}
    if args.archive:
        archive = args.archive.absolute()
        _validate_output_parent(archive)
        if archive.exists() or archive.is_symlink():
            raise ValueError("Use a fresh archive path.")
        entries = {row["path"]: row for row in manifest["files"]}
        entries["OWNED_FILES.json"] = None
        with archive.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as packed:
            for relative, fact in sorted(entries.items()):
                content = (destination / relative).read_bytes()
                if fact and (len(content) != fact["size"] or hashlib.sha256(content).hexdigest() != fact["sha256"]):
                    raise ValueError("A candidate file changed before packaging.")
                if fact is None and json.loads(content) != manifest:
                    raise ValueError("The ownership manifest changed before packaging.")
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                packed.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        verify_layout(destination)
        result["archive"] = archive.name
        result["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        result["archive_bytes"] = archive.stat().st_size
    print(json.dumps(result))


if __name__ == "__main__":
    main()
