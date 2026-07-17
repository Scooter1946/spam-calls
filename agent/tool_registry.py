"""Runtime tool registry (implements ToolRegistryPort).

Scans ``<tools_dir>/*.manifest.json``, validates each :class:`ToolManifest`,
imports the tool's ``entrypoint`` from the ``.py`` file that sits next to the
manifest, and indexes callable handles by capability.

Loading is done by file path (not by package import) so the registry works
identically for the real ``generated_tools/`` directory and for the temporary
directories P1's tests use. ``reload()`` clears previously loaded dynamic modules
and rescans, so a capability that did not exist before a merge becomes available
after ``reload()``. Invalid manifests become explicit failed observations rather
than crashes. The orchestrator never imports the generated tool directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from contracts.models import ToolManifest

DEFAULT_TOOLS_DIR = "generated_tools"


@dataclass
class ToolHandle:
    """A callable handle to a loaded tool, plus its manifest."""

    manifest: ToolManifest
    run: Callable[..., dict[str, Any]]

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.run(*args, **kwargs)


@dataclass
class LoadFailure:
    manifest_path: str
    error: str


class ToolRegistry:
    """Capability-indexed registry of generated tools loaded from disk."""

    def __init__(self, tools_dir: str | Path = DEFAULT_TOOLS_DIR, artifacts: Any | None = None) -> None:
        self.tools_dir = Path(tools_dir)
        self._artifacts = artifacts
        self._by_capability: dict[str, ToolHandle] = {}
        self._failures: list[LoadFailure] = []
        self._loaded_module_names: list[str] = []

    # -- ToolRegistryPort -------------------------------------------------- #

    def reload(self) -> None:
        """Clear previously loaded dynamic modules and rescan the tools dir."""

        for name in self._loaded_module_names:
            sys.modules.pop(name, None)
        self._loaded_module_names.clear()
        self._by_capability.clear()
        self._failures.clear()
        importlib.invalidate_caches()

        if not self.tools_dir.is_dir():
            return

        for manifest_path in sorted(self.tools_dir.glob("*.manifest.json")):
            try:
                self._load_one(manifest_path)
            except Exception as exc:  # noqa: BLE001 - turn into explicit observation
                failure = LoadFailure(manifest_path=str(manifest_path), error=repr(exc))
                self._failures.append(failure)
                if self._artifacts is not None:
                    self._artifacts.append_event(
                        {
                            "type": "error",
                            "where": "tool_registry.reload",
                            "manifest": failure.manifest_path,
                            "error": failure.error,
                        }
                    )

    def find(self, capability: str) -> ToolHandle | None:
        """Return a callable handle for ``capability``, or ``None`` if absent."""

        return self._by_capability.get(capability)

    # -- observability ----------------------------------------------------- #

    @property
    def failures(self) -> list[LoadFailure]:
        return list(self._failures)

    def capabilities(self) -> list[str]:
        return sorted(self._by_capability)

    # -- internals --------------------------------------------------------- #

    def _load_one(self, manifest_path: Path) -> None:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ToolManifest.model_validate(data)

        module_qual, sep, func_name = manifest.entrypoint.partition(":")
        if not sep or not func_name:
            raise ValueError(
                f"entrypoint must be 'module.path:function', got {manifest.entrypoint!r}"
            )

        module_basename = module_qual.rsplit(".", 1)[-1]
        module_file = self.tools_dir / f"{module_basename}.py"
        if not module_file.is_file():
            raise FileNotFoundError(f"tool module not found: {module_file}")

        unique_name = f"_pitchloop_tool__{manifest.capability}__{module_basename}"
        spec = importlib.util.spec_from_file_location(unique_name, module_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {module_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = module
        self._loaded_module_names.append(unique_name)
        spec.loader.exec_module(module)

        fn = getattr(module, func_name, None)
        if not callable(fn):
            raise AttributeError(
                f"entrypoint function {func_name!r} not found in {module_file}"
            )

        self._by_capability[manifest.capability] = ToolHandle(manifest=manifest, run=fn)
