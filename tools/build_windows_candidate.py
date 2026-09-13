"""Build a fixed-commit, first-party Python/Tk Windows installer candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "src"))
from neutral_tts.install_contract import create_test_layout, verify_layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
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
        manifest = create_test_layout(source, destination, selected,
            candidate_id="candidate-" + commit[:12], source_commit=commit, source_tree=tree,
            app_root_name="neutral-tts-workbench", verification_scope="LOCAL_WINDOWS_SOURCE_CANDIDATE")
    verify_layout(destination)
    print(json.dumps({"commit": commit, "tree": tree, "file_count": manifest["file_count"],
                      "files_sha256": manifest["files_sha256"], "signature": "unsigned",
                      "runtime_included": False, "fresh_windows_verified": False}))


if __name__ == "__main__":
    main()
