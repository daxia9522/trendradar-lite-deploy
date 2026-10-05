"""Immutable, content-addressed radix indexes on the generic snapshot API.

The manifest holds roots, not ever-growing identity/report/window lists. Every
node is bounded; missing/corrupt nodes fail closed. Updates precede manifest CAS,
so a losing writer can leave unreachable objects but cannot lose committed keys.
"""
from __future__ import annotations

import hashlib
import json

from trendradar.storage.publication import PublicationError

LEAF_ENTRIES = 512
NODE_BYTES = 128 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


class SnapshotIndex:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _path(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def _read(self, root):
        if root is None:
            return {"type": "radix-v1", "entries": {}}
        node = self.store.get_snapshot(root)
        if (node.get("type") != "radix-v1" or len(encoded(node)) > NODE_BYTES
                or root != "idx-" + hashlib.sha256(encoded(node)).hexdigest()
                or (("entries" in node) == ("children" in node))):
            raise PublicationError("Invalid publication index node")
        return node

    def _write(self, node):
        data = encoded(node)
        if len(data) > NODE_BYTES:
            raise PublicationError("Publication index node exceeds bound")
        root = "idx-" + hashlib.sha256(data).hexdigest()
        self.store.put_snapshot(root, node)
        return root

    def get_many(self, root, keys):
        paths = {key: self._path(key) for key in keys}

        def visit(reference, requested, depth):
            if not requested or reference is None:
                return {}
            node = self._read(reference)
            if "entries" in node:
                return {key: node["entries"][key] for key in requested if key in node["entries"]}
            if depth >= 64:
                raise PublicationError("Invalid publication index depth")
            found = {}
            groups = {}
            for key in requested:
                groups.setdefault(paths[key][depth], []).append(key)
            for nibble, group in groups.items():
                found.update(visit(node["children"].get(nibble), group, depth + 1))
            return found
        return visit(root, list(paths), 0)

    def update(self, root, entries):
        def visit(reference, changes, depth):
            if not changes:
                return reference
            node = self._read(reference)
            if "entries" in node:
                merged = {**node["entries"], **changes}
                candidate = {"type": "radix-v1", "entries": merged}
                if len(merged) <= LEAF_ENTRIES and len(encoded(candidate)) <= NODE_BYTES:
                    if merged == node["entries"]:
                        return reference
                    return self._write(candidate)
                if depth >= 64:
                    raise PublicationError("Publication index entry exceeds bound")
                changes = merged
                node = {"type": "radix-v1", "children": {}}
            children = dict(node["children"])
            groups = {}
            for key, value in changes.items():
                groups.setdefault(self._path(key)[depth], {})[key] = value
            for nibble, group in groups.items():
                children[nibble] = visit(children.get(nibble), group, depth + 1)
            candidate = {"type": "radix-v1", "children": children}
            return reference if candidate == node and reference else self._write(candidate)
        return visit(root, entries, 0)

    def items(self, root):
        """Stream bounded leaves; callers never fetch an unbounded object."""
        if root is None:
            return
        node = self._read(root)
        if "entries" in node:
            yield from node["entries"].items()
        else:
            for child in node["children"].values():
                yield from self.items(child)

    def union(self, old, captured):
        if captured is None:
            return old
        if old is None or old == captured:
            # Roots were staged earlier, but an unreadable/missing descendant
            # must still stop submission rather than publish an unusable index.
            for _, value in self.items(captured):
                if value is not True:
                    raise PublicationError("Invalid identity index value")
            return captured
        # Capture roots hold only this report's observations, not cumulative
        # history. Batch leaves to bound working memory during adoption/migration.
        batch = {}
        for key, value in self.items(captured):
            if value is not True:
                raise PublicationError("Invalid identity index value")
            batch[key] = True
            if len(batch) >= 8192:
                old = self.update(old, batch)
                batch.clear()
        return self.update(old, batch)
