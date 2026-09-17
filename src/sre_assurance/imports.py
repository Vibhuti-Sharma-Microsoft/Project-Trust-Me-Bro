from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import CorpusManifest

TABLES = {"customEvents", "dependencies", "genAIContent", "traces", "exceptions", "requests"}


class ImportFailure(ValueError):
    """The local evidence contract is incomplete, ambiguous, or invalid."""


def safe_path(root: Path, relative: str) -> Path:
    value = Path(relative.replace("\\", "/"))
    if value.is_absolute() or value.drive or ":" in relative:
        raise ImportFailure("Corpus paths must be relative to the manifest directory")
    resolved = (root.resolve() / value).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ImportFailure("A corpus path escapes the manifest directory")
    return resolved


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_manifest(path: Path) -> CorpusManifest:
    return CorpusManifest.model_validate_json(path.read_text(encoding="utf-8-sig"))


def _json_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict) and "tables" in value:
        tables = value["tables"]
        if not isinstance(tables, list) or len(tables) != 1:
            raise ImportFailure("A query JSON export must contain exactly one result table")
        table = tables[0]
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
            raise ImportFailure("A query result table must have columns and rows arrays")
        if not all(isinstance(column, dict) and isinstance(column.get("name"), str) and column["name"] for column in table["columns"]):
            raise ImportFailure("Each query result column must have a nonempty name")
        columns = [column["name"] for column in table["columns"]]
        if len(columns) != len(set(columns)):
            raise ImportFailure("Query result column names must be unique")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in table["rows"]):
            raise ImportFailure("Export row/column lengths do not agree")
        records = [dict(zip(columns, row, strict=True)) for row in table["rows"]]
    else:
        raise ImportFailure("Expected a JSON record array or one Query API result table")
    if not all(isinstance(row, dict) for row in records):
        raise ImportFailure("Every telemetry row must be an object")
    return records


def load_table(path: Path, table: str) -> list[dict[str, Any]]:
    if table not in TABLES:
        raise ImportFailure(f"Unsupported telemetry table: {table}")
    if path.suffix.lower() == ".csv":
        csv.field_size_limit(8_000_000)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows: list[dict[str, Any]] = list(csv.DictReader(stream))
    elif path.suffix.lower() == ".jsonl":
        rows = _json_records([json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()])
    else:
        rows = _json_records(json.loads(path.read_text(encoding="utf-8-sig")))
    result = []
    for index, row in enumerate(rows):
        if any(not isinstance(key, str) for key in row):
            raise ImportFailure(f"{table} row {index + 1}: malformed column layout")
        if "timestamp" not in row:
            raise ImportFailure(f"{table} row {index + 1}: missing ISO timestamp; aggregate inventories are not raw logs")
        cd = row.get("customDimensions", {})
        if isinstance(cd, str):
            try:
                cd = json.loads(cd) if cd.strip() else {}
            except json.JSONDecodeError as exc:
                raise ImportFailure(f"{table} row {index + 1}: customDimensions is not valid JSON") from exc
        if not isinstance(cd, dict):
            raise ImportFailure(f"{table} row {index + 1}: customDimensions must be an object")
        result.append({**row, "customDimensions": cd, "_table": table, "_local_row": index + 1})
    return result
