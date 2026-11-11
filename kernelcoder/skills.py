"""Discover a complete skill catalog; load instructions only on activation."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import yaml

from .tools.base import Tool


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    source: str
    fingerprint: str


class SkillLoader:
    """First directory wins; defaults are project skills, then user skills."""

    def __init__(self, directories: list[Path] | None = None):
        self.directories = [Path(p).absolute() for p in (directories if directories is not None else [
            Path.cwd() / ".kernelcoder" / "skills",
            Path.home() / ".kernelcoder" / "skills",
        ])]
        self.skills: dict[str, Skill] = {}
        self.diagnostics: list[str] = []
        self.scan()

    @staticmethod
    def _parse(content: str, directory_name: str) -> tuple[str, str]:
        lines = content.lstrip("\ufeff").splitlines()
        if lines and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
            if end is None:
                raise ValueError("Unclosed YAML frontmatter")
            metadata = yaml.safe_load("\n".join(lines[1:end]))
            if not isinstance(metadata, dict):
                raise ValueError("Frontmatter must be a mapping")
            name = metadata.get("name", directory_name)
            description = metadata.get("description")
        else:
            # Keep the original heading-only format compatible with local skills.
            name = directory_name
            description = next((line[2:] for line in lines if line.startswith("# ")), "")
        if not isinstance(name, str) or not name.strip() or any(c.isspace() or c in "/\\" for c in name):
            raise ValueError("Skill name must be a nonempty command name without spaces or paths")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("A nonempty description is required")
        return name, " ".join(description.split())

    @staticmethod
    def _read(path: Path, root: Path) -> str:
        # Recheck at activation, including Windows junctions and replaced files.
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Skill path escapes its configured directory or is a symbolic link")
        return path.read_text(encoding="utf-8-sig")

    def scan(self) -> None:
        skills, diagnostics = {}, []
        for directory in self.directories:
            if not directory.is_dir():
                continue
            try:
                entries = sorted(directory.iterdir())
            except OSError as error:
                diagnostics.append(f"{directory}: {error}")
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                path = entry / "SKILL.md"
                if not path.is_file():
                    continue
                try:
                    content = self._read(path, directory)
                    name, description = self._parse(content, entry.name)
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
                    diagnostics.append(f"{path}: {error}")
                    continue
                if name in skills:
                    diagnostics.append(f"{path}: {name!r} shadowed by {skills[name].path}")
                    continue
                if name != entry.name:
                    diagnostics.append(f"{path}: declared name {name!r} differs from directory {entry.name!r}")
                skills[name] = Skill(name, description, path, str(directory), self._fingerprint(content))
        self.skills, self.diagnostics = skills, diagnostics

    @staticmethod
    def _fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def summary(self) -> str:
        """Never silently hide skills or truncate their routing descriptions."""
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in self.skills.values())

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if skill is None:
            return f"Error: unknown skill {name!r}. Use /skills to list available skills."
        try:
            content = self._read(skill.path, Path(skill.source))
            actual_name, description = self._parse(content, skill.path.parent.name)
            if actual_name != name or description != skill.description:
                return f"Error: skill {name!r} metadata changed; run /skills reload before loading it."
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            return f"Error: cannot read skill {name!r}: {error}"
        version = self._fingerprint(content)
        return f"Skill: {name}\nVersion: {version}\nBase directory: {skill.path.parent}\n\n{content}"


class SkillTool(Tool):
    name = "load_skill"
    description = (
        "Load a skill's complete instructions before using it in the current user turn. "
        "Choose from the available skills in the system prompt. "
        "Resources referenced by the skill are read separately when needed."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"skill": {"type": "string", "description": "Exact skill name from the catalog"}},
        "required": ["skill"],
    }

    def __init__(self, loader: SkillLoader):
        self.loader = loader

    def execute(self, skill: str) -> str:
        return self.loader.load(skill)
