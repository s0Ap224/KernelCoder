"""File pattern matching."""

from pathlib import Path
from typing import ClassVar

from ..workspace import CURRENT_WORKSPACE, display_path, tool_path
from .base import Tool


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern. "
        "Supports ** for recursive matching (e.g. '**/*.py')."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: cwd)",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".") -> str:
        try:
            base = tool_path(path)
            if not base.exists():
                return f"Error: {path} not found"
            if not base.is_dir():
                return f"Error: {path} is not a directory"

            workspace = CURRENT_WORKSPACE.get()
            if workspace:
                if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                    return "Error: glob pattern must stay inside the project"
                hits = [workspace.tree / n for n in workspace.scan()]
                hits = [p for p in hits if base in p.parents and
                        (p.relative_to(base).match(pattern) or
                         (pattern.startswith("**/") and p.relative_to(base).match(pattern[3:])))]
            else:
                hits = list(base.glob(pattern))
            # 找出所有匹配项
            hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
            # 按照最后修改时间排序

            total = len(hits)
            shown = hits[:100]
            lines = [display_path(h) for h in shown]
            result = "\n".join(lines)

            if total > 100:
                result += f"\n... ({total} matches, showing first 100)"
            return result or "No files matched."
        except Exception as e:  # noqa: BLE001
            # boundary: the agent gets an error string, not a traceback
            return f"Error: {e}"
