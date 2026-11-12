"""Compact, item-level undo/redo history.

Only changed objects are stored. Simple edits are patched in place; structural
changes rebuild only their affected top-level branch instead of the whole board.
"""
from dataclasses import dataclass


def flatten(items, parent=None, top=None):
    result = {}
    for data in items:
        if not isinstance(data, dict) or not data.get("id"):
            continue
        uid = data["id"]
        top_id = top or uid
        flat = {k: v for k, v in data.items() if k != "children"}
        flat["_parent"] = parent
        flat["_top"] = top_id
        result[uid] = flat
        if data.get("kind") == "group":
            result.update(flatten(data.get("children", []), uid, top_id))
    return result


def top_map(items):
    return {d.get("id"): d for d in items if isinstance(d, dict) and d.get("id")}


@dataclass
class HistoryEntry:
    simple_before: dict
    simple_after: dict
    rebuild_before: dict
    rebuild_after: dict

    @property
    def empty(self):
        return not (self.simple_after or self.rebuild_after or
                    self.simple_before or self.rebuild_before)


class HistoryManager:
    def __init__(self, limit=100):
        self.limit = limit
        self.undo_stack = []
        self.redo_stack = []
        self.current = []

    def reset(self, state):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.current = state

    def record(self, state):
        old_flat, new_flat = flatten(self.current), flatten(state)
        old_tops, new_tops = top_map(self.current), top_map(state)
        ids = set(old_flat) | set(new_flat)
        changed = {uid for uid in ids if old_flat.get(uid) != new_flat.get(uid)}
        if not changed:
            return None

        affected_tops = set()
        for uid in changed:
            before, after = old_flat.get(uid), new_flat.get(uid)
            structural = before is None or after is None
            if before and after:
                structural = any(before.get(k) != after.get(k)
                                 for k in ("_parent", "_top", "kind", "path"))
            if structural:
                if before:
                    affected_tops.add(before["_top"])
                if after:
                    affected_tops.add(after["_top"])

        simple_before, simple_after = {}, {}
        for uid in changed:
            before, after = old_flat.get(uid), new_flat.get(uid)
            top_id = (after or before)["_top"]
            if top_id in affected_tops:
                continue
            if before is not None:
                simple_before[uid] = before
            if after is not None:
                simple_after[uid] = after

        rebuild_before = {uid: old_tops.get(uid) for uid in affected_tops}
        rebuild_after = {uid: new_tops.get(uid) for uid in affected_tops}
        entry = HistoryEntry(simple_before, simple_after, rebuild_before, rebuild_after)
        self.undo_stack.append(entry)
        del self.undo_stack[:-self.limit]
        self.redo_stack.clear()
        self.current = state
        return entry

    def undo(self):
        if not self.undo_stack:
            return None
        entry = self.undo_stack.pop()
        self.redo_stack.append(entry)
        return entry, "before"

    def redo(self):
        if not self.redo_stack:
            return None
        entry = self.redo_stack.pop()
        self.undo_stack.append(entry)
        return entry, "after"

    def committed(self, state):
        self.current = state
