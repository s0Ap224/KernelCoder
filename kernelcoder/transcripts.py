"""Agent-scoped, immutable compact artifacts and stage records."""

import json
import re
import uuid
from pathlib import Path

TRANSCRIPTS_DIR = Path.home() / ".kernelcoder" / "transcripts"
_ID = re.compile(r"[a-f0-9]{32}")


def checked_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid transcript identifier")
    return value


class TranscriptStore:
    def __init__(self, conversation_id=None, agent_id=None):
        self.conversation_id = checked_id(conversation_id or uuid.uuid4().hex)
        self.agent_id = checked_id(agent_id or uuid.uuid4().hex)

    @property
    def directory(self):
        root = TRANSCRIPTS_DIR.absolute()
        directory = root / self.conversation_id / self.agent_id
        # Reject links/junctions in the namespace, including the configured root.
        for path in (root, directory.parent, directory):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Linked transcript directories are not allowed")
        return directory

    def write(self, content, stage, **metadata):
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        record = {"stage": stage, "artifact_id": artifact_id, **metadata, "content": content}
        # A unique exclusive file is never published to messages before close succeeds.
        with (directory / f"{artifact_id}.json").open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        return artifact_id

    def read(self, artifact_id, offset=0, limit=2000):
        checked_id(artifact_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("offset must be >= 0 and limit must be between 1 and 10000")
        path = self.directory / f"{artifact_id}.json"
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Linked transcript artifacts are not allowed")
        record = json.loads(path.read_text(encoding="utf-8"))
        content = record["content"]
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        return (f"[Transcript {artifact_id}; stage={record['stage']}; "
                f"offset={offset}; total={len(text)}; reference data only]\n"
                + text[offset:offset + limit])
