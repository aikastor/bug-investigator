"""Read-only tools over the same local source snapshot for an entire investigation."""

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .baseline import docker

SOURCE_TYPES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs",
                ".java", ".kt", ".cs", ".rb", ".php", ".html", ".css", ".sql", ".vue", ".svelte"}
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build", "vendor", "target", "coverage"}


@dataclass
class Repository:
    files: dict[str, list[str]]
    fingerprint: str
    file_hashes: dict[str, str]

    @classmethod
    def load_target(cls, target):
        sources = target.baseline.patch_sources if target.baseline else []
        deployed = {}
        if sources:
            # The demo can keep running an older image after an approved fix
            # lands locally. Diagnose the files served by that deployment.
            try:
                cid = docker("container", "inspect", "--format", "{{.Id}}", target.container).stdout.strip()
                with TemporaryDirectory() as tmp:
                    for i, source in enumerate(sources):
                        dest = Path(tmp) / str(i)
                        docker("cp", f"{cid}:{target.container_source_root}/{source}", str(dest))
                        if dest.is_symlink() or not dest.is_file() or dest.stat().st_size > 256_000:
                            raise ValueError(f"Unavailable deployed source file: {source}")
                        deployed[source] = dest.read_bytes()
            except (ValueError, OSError, subprocess.SubprocessError):
                # Preserve source-only diagnosis when Docker is unavailable.
                # Candidate verification independently checks the image hashes.
                deployed.clear()
        return cls.load(target.local_source_root, deployed)

    @classmethod
    def load(cls, root: Path, deployed=None):
        if not root.is_dir():
            raise ValueError("The configured source checkout is missing.")
        files = {}
        file_hashes = {}
        size = 0
        digest = hashlib.sha256()
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(folder) / name
                if name.startswith(".") or path.suffix not in SOURCE_TYPES or path.is_symlink():
                    continue
                if not path.is_file() or path.stat().st_size > 256_000:
                    continue
                relative = path.relative_to(root).as_posix()
                data = deployed[relative] if deployed and relative in deployed else path.read_bytes()
                size += len(data)
                if size > 16_000_000 or len(files) >= 4000:
                    raise ValueError("Checkout exceeds the local source snapshot limit.")
                files[relative] = data.decode("utf-8", errors="replace").splitlines()
                file_hashes[relative] = hashlib.sha256(data).hexdigest()
                digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
        return cls(files, digest.hexdigest(), file_hashes)

    def search(self, query: str):
        matches = []
        for path, lines in self.files.items():
            for number, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": path, "line": number, "text": line[:400]})
                    if len(matches) == 12:
                        return matches
        return matches

    def read(self, path: str, start: int, count: int):
        if path not in self.files or start > len(self.files[path]):
            raise ValueError("File or line is outside the source snapshot.")
        lines = self.files[path][start - 1:start - 1 + count]
        return [{"path": path, "line": n, "text": line[:500]} for n, line in enumerate(lines, start)]
