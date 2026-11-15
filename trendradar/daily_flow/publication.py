"""Local cutoff for publication-relative novelty."""

import json
from pathlib import Path


class PublicationBeacon:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "publication.json"

    def load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(state, dict):
            raise ValueError("Invalid publication beacon")
        return state

    def advance(self, capture, window):
        state = self.load()
        for kind, coverage in capture["coverage"].items():
            if coverage["complete"]:
                state[kind] = coverage["through"]
        state["window"] = window
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)
