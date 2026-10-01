"""Process-wide cache of the loaded semantic layer, reloaded when files change."""

from __future__ import annotations

import threading
from pathlib import Path

from app.semantic.loader import SemanticBundle, load_semantic_layer


class SemanticLayerRegistry:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._bundle: SemanticBundle | None = None

    def _current_signature(self) -> tuple[tuple[str, int, int], ...]:
        if not self._root.is_dir():
            return ()
        files = sorted(p for p in self._root.rglob("*") if p.suffix in {".yaml", ".yml"})
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)

    def get(self) -> SemanticBundle:
        """Return the bundle, reloading if any YAML file changed. Raises SemanticLayerError."""
        signature = self._current_signature()
        with self._lock:
            if self._bundle is None or signature != self._signature:
                self._bundle = load_semantic_layer(self._root)
                self._signature = signature
            return self._bundle
