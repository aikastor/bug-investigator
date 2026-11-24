"""Read-only tools over the same local source snapshot for an entire investigation."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

SOURCE_TYPES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs",
                ".java", ".kt", ".cs", ".rb", ".php", ".html", ".css", ".sql", ".vue", ".svelte"}
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build", "vendor", "target", "coverage"}


@dataclass
class Repository:
    files: dict[str, list[str]]
    fingerprint: str
    file_hashes: dict[str, str]

    @classmethod
    def load(cls, root: Path):
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
                data = path.read_bytes()
                size += len(data)
                if size > 16_000_000 or len(files) >= 4000:
                    raise ValueError("Checkout exceeds the local source snapshot limit.")
                relative = path.relative_to(root).as_posix()
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
