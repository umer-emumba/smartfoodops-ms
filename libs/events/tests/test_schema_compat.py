"""Each event's JSON Schema must stay BACKWARD compatible with the snapshot in
libs/events/schemas/ — the schema already registered and in use. A fast,
offline pre-check; Schema Registry stays the authority at deploy. With
closed models, adding an optional field is fine; removing a field, making a
field required, or narrowing its type is not. Making a field that has a
default required passes the registry but fails here — stricter on purpose.

After an intentional, compatible change, refresh the snapshots:
    UPDATE_SCHEMA_SNAPSHOTS=1 uv run pytest libs/events/tests/test_schema_compat.py
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from sfo_events import EventType
from sfo_events.payloads import envelope_json_schema

SNAPSHOTS = Path(__file__).resolve().parent.parent / "schemas"
Schema = dict[str, Any]


def _resolve(schema: Schema, root: Schema) -> Schema:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return _resolve(node, root)


def _types(schema: Schema) -> set[str]:
    if "const" in schema:
        return {f"const:{schema['const']}"}
    if "anyOf" in schema:
        return set().union(*(_types(s) for s in schema["anyOf"]))
    kind = schema.get("type")
    return {kind} if isinstance(kind, str) else set(kind or [])


def backward_problems(
    old: Schema, new: Schema, old_root: Schema, new_root: Schema, path: str = ""
) -> list[str]:
    """Why data written with `old` might not validate against `new`."""
    old, new = _resolve(old, old_root), _resolve(new, new_root)
    problems: list[str] = []
    if new.get("additionalProperties") is False and old.get("additionalProperties") is not False:
        problems.append(f"{path or '<root>'}: closed an open model")
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    for name, old_prop in old_props.items():
        here = f"{path}{name}"
        if name not in new_props:
            if new.get("additionalProperties") is False:
                problems.append(f"{here}: removed")
            continue
        o, n = _resolve(old_prop, old_root), _resolve(new_props[name], new_root)
        if o.get("type") == "object" and n.get("type") == "object":
            problems += backward_problems(o, n, old_root, new_root, f"{here}.")
        elif o.get("type") == "array" and n.get("type") == "array":
            problems += backward_problems(
                o.get("items", {}), n.get("items", {}), old_root, new_root, f"{here}[]."
            )
        elif not _types(o) <= _types(n):
            problems.append(f"{here}: type {sorted(_types(o))} → {sorted(_types(n))}")
    for name in sorted(set(new.get("required", [])) - set(old.get("required", []))):
        problems.append(f"{path}{name}: newly required")
    return problems


def test_checker_catches_breaking_changes() -> None:
    old = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    added_optional = {**old, "properties": {**old["properties"], "c": {"type": "string"}}}
    assert backward_problems(old, added_optional, old, added_optional) == []
    removed = {**old, "properties": {"a": {"type": "string"}}}
    assert backward_problems(old, removed, old, removed) == ["b: removed"]
    required = {**old, "required": ["a", "b"]}
    assert backward_problems(old, required, old, required) == ["b: newly required"]
    narrowed = {**old, "properties": {**old["properties"], "a": {"type": "integer"}}}
    assert backward_problems(old, narrowed, old, narrowed) == ["a: type ['string'] → ['integer']"]


@pytest.mark.parametrize("event_type", list(EventType))
def test_schema_is_backward_compatible_with_snapshot(event_type: EventType) -> None:
    current = envelope_json_schema(event_type)
    snapshot = SNAPSHOTS / f"{event_type.value}.json"
    if os.environ.get("UPDATE_SCHEMA_SNAPSHOTS") == "1":
        SNAPSHOTS.mkdir(exist_ok=True)
        snapshot.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    assert snapshot.exists(), f"No snapshot for {event_type} — see this module's docstring"
    old = json.loads(snapshot.read_text())
    assert backward_problems(old, current, old, current) == []
