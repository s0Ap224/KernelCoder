"""Discover small, local SKILL.md files and load their instructions on demand.

Only names and descriptions enter the initial prompt. The full document is
returned by load_skill when the model decides it is relevant.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .tools.base import Tool


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


class SkillLoader:
    """Discover project skills first, then user skills with the same name."""

    def __init__(self, directories: list[Path] | None = None):
        self.directories = directories if directories is not None else [
            Path.cwd() / ".kernelcoder" / "skills",
            Path.home() / ".kernelcoder" / "skills",
        ]
        self.skills: dict[str, Skill] = {}
        self.scan()

    def scan(self) -> None:
        self.skills.clear()
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                # Skills are local directories, never links to arbitrary files.
                if not entry.is_dir() or entry.is_symlink() or entry.name in self.skills:
                    continue
                path = entry / "SKILL.md"
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                description = self._description(content)
                if description:
                    self.skills[entry.name] = Skill(entry.name, description, path)

    @staticmethod
    def _description(content: str) -> str:
        """Read a single-line description; use the first heading as a fallback."""
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                key, separator, value = line.partition(":")
                if separator and key.strip() == "description":
                    return value.strip().strip('"\'').strip()
        for line in lines:
            if line.startswith("# "):
                return line[2:].strip()
        return ""

    def summary(self, char_budget: int = 3000) -> str:
        """Keep the initial prompt small even when many skills exist."""
        lines = []
        remaining = max(0, char_budget)
        for skill in self.skills.values():
            prefix = f"- {skill.name}: "
            if len(prefix) > remaining:
                break
            description = skill.description.replace("\n", " ")[:250]
            line = prefix + description[: remaining - len(prefix)]
            lines.append(line)
            remaining -= len(line) + 1
        return "\n".join(lines)

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if skill is None:
            return f"Error: unknown skill {name!r}. Available: {', '.join(self.skills) or '(none)'}"
        try:
            content = skill.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return f"Error: cannot read skill {name!r}: {exc}"
        return f"Skill: {name}\nBase directory: {skill.path.parent}\n\n{content}"


class SkillTool(Tool):
    name = "load_skill"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"skill": {"type": "string", "description": "Skill directory name"}},
        "required": ["skill"],
    }

    def __init__(self, loader: SkillLoader):
        self.loader = loader
        self.description = (
            "Load the full SKILL.md instructions for a relevant skill before using it. "
            "Available skills (name: description):\n" + loader.summary()
        )

    def execute(self, skill: str) -> str:
        return self.loader.load(skill)
