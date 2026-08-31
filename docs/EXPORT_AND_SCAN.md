# Clean export and public-surface scan

The exporter reads a private, exact source manifest and a private positive
allowlist. It creates a previously absent staging directory and copies only
manifested regular files whose size and SHA-256 match. It refuses path
traversal, absolute/drive/UNC paths, reparse points, containment overlap, and
overwrites.

Example shape (paths are intentionally environment-relative):

```powershell
python tools/clean_export.py `
  --source-root <approved-source-root> `
  --destination-root .staging/<candidate>/src/neutral_tts `
  --manifest .private-compliance/export-manifest.json `
  --allowlist .private-compliance/export-allowlist.json
```

Scan staging before promotion:

```powershell
python tools/scan_public_surface.py `
  --root .staging/<candidate> `
  --allowlist .private-compliance/staging-source-scan-allowlist.json `
  --private-policy .private-compliance/public-scan-policy.json
```

The partial staging allowlist deliberately has no required-disclosure list,
because this scan covers only the hash-pinned source subset. The final public
allowlist does require the notice, release NO-GO, and license-matrix paths;
their absence fails worktree, archive, and Git scans.

At a clean commit, scan the exact Git tree and all reachable history without
including ignored private control files:

```powershell
python tools/scan_public_surface.py `
  --root . `
  --allowlist compliance/source-asset-allowlist.json `
  --private-policy .private-compliance/public-scan-policy.json `
  --git-history --git-only
```

Any future archive must also be supplied with `--archive`. Archive members are
checked as the same logical public paths; nested archives, links, special
members, traversal, unreadable content, and unclassified payloads fail.
Use `--archive-only` when the archive is the exact independently scanned
surface and the `--root` directory is only a safe scanner anchor.

The scan report is evidence about the exact input only. It is not a license
decision, redistribution approval, binary acceptance, or Production evidence.
