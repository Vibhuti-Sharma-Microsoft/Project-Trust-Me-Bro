"""Reject known private paths/roster IDs in Git candidates; not a general secret scanner."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

PRIVATE_ROOTS = {".venv", ".cache", "data", "out", "dist", "build", ".pytest_cache", ".vscode", ".idea"}
PRIVATE_SUFFIXES = {".pem", ".key", ".pfx", ".p12"}


def _git(root: Path, *arguments: str) -> bytes:
    process = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False)
    if process.returncode:
        raise ValueError(f"Git command failed: {' '.join(arguments[:2])}")
    return process.stdout


def check_publish(root: Path) -> list[str]:
    root = root.resolve()
    git_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if git_root != root:
        raise ValueError("Run this check at the standalone repository root, not its parent workspace")
    tracked = {name.decode() for name in _git(root, "ls-files", "--cached", "-z").split(b"\0") if name}
    untracked = {name.decode() for name in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name}
    roster = root / "data" / "incident-roster.local.json"
    identifiers: list[bytes] = []
    if roster.exists():
        value = json.loads(roster.read_text(encoding="utf-8-sig"))
        identifiers = [str(item).encode() for item in value.get("incident_ids", [])]
    findings = []
    for name in sorted(tracked | untracked):
        path = PurePosixPath(name)
        private = (
            path.parts[0] in PRIVATE_ROOTS
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or (path.name.startswith(".env") and path.name != ".env.example")
            or (path.parts[0] == "config" and path.name.endswith(".local.json"))
            or (len(path.parts) == 1 and path.suffix.lower() in {".csv", ".jsonl"})
        )
        if private:
            findings.append(f"Private/generated path is publishable: {name}")
            continue
        physical = root.joinpath(*path.parts)
        if physical.is_symlink() or not physical.resolve().is_relative_to(root):
            findings.append(f"Symlink or escaping candidate requires review: {name}")
            continue
        versions = []
        if name in tracked:
            versions.append(("index", _git(root, "show", f":{name}")))
        if physical.is_file():
            versions.append(("working tree", physical.read_bytes()))
        for location, data in versions:
            if any(identifier and identifier in data for identifier in identifiers):
                findings.append(f"Private roster identifier appears in {location}: {name}")
    return findings


def main() -> int:
    try:
        findings = check_publish(Path(__file__).resolve().parent.parent)
    except (ValueError, OSError) as exc:
        print(f"Publish check failed: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Publish candidates contain no known private paths or local roster IDs. Manual secret/privacy review is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
