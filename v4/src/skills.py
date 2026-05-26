"""Custom skills system — loads markdown skill files on demand.

Skills are markdown files in the skills/ directory. The model calls
load_skill(name) and gets back the full instructions as context.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsManager:
    """Discovers and loads skill markdown files."""

    def __init__(self, skills_dir: Path | None = None):
        self._dir = skills_dir or SKILLS_DIR

    def list_skills(self) -> list[str]:
        """Return available skill names."""
        if not self._dir.exists():
            return []
        skills = []
        for p in sorted(self._dir.iterdir()):
            if p.is_dir() and (p / "SKILL.md").exists():
                skills.append(p.name)
            elif p.suffix == ".md" and p.stem != "README":
                skills.append(p.stem)
        return skills

    def load(self, name: str) -> dict:
        """Load a skill by name. Returns its content or an error."""
        name = name.strip().lower()
        if not name:
            return {"error": "Skill name is required.", "available": self.list_skills()}

        # Try directory-based skill first
        dir_path = self._dir / name / "SKILL.md"
        if dir_path.exists():
            content = dir_path.read_text(encoding="utf-8")
            logger.info("Loaded skill: %s (%d chars)", name, len(content))
            return {"skill": name, "instructions": content}

        # Try flat file
        file_path = self._dir / f"{name}.md"
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            logger.info("Loaded skill: %s (%d chars)", name, len(content))
            return {"skill": name, "instructions": content}

        return {
            "error": f"Skill '{name}' not found.",
            "available": self.list_skills(),
        }
