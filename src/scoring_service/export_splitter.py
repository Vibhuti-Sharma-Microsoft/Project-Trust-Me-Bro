from __future__ import annotations

import csv
from datetime import UTC, datetime
from collections import defaultdict
from pathlib import Path

from .imports import TABLES


def _timestamp(row: dict[str, str], path: Path, row_number: int) -> str:
    value = row.get("timestamp") or row.get("timestamp [UTC]") or ""
    if not value:
        raise ValueError(f"{path}:{row_number}: missing timestamp")
    if "T" in value and value.endswith("Z"):
        return value
    try:
        date, fraction_and_period = value.split(".", 1)
        fraction, period = fraction_and_period.rsplit(" ", 1)
        microseconds = (fraction + "000000")[:6]
        parsed = datetime.strptime(f"{date}.{microseconds} {period}", "%m/%d/%Y, %I:%M:%S.%f %p")
    except ValueError:
        raise ValueError(f"{path}:{row_number}: unsupported portal timestamp {value!r}") from None
    return parsed.replace(tzinfo=UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def split_exports(inputs: list[Path], incident_id: str, directory: Path) -> dict[str, int]:
    if not inputs:
        raise ValueError("At least one export input is required")
    if directory.exists() and any(directory.glob("*.csv")):
        raise FileExistsError("Destination already contains CSV files; existing exports are never overwritten")

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    columns: dict[str, list[str]] = {}
    for path in inputs:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            timestamp_field = (
                "timestamp" if reader.fieldnames and "timestamp" in reader.fieldnames
                else "timestamp [UTC]" if reader.fieldnames and "timestamp [UTC]" in reader.fieldnames
                else None
            )
            if reader.fieldnames is None or not {"IncidentId", "SourceTable"} <= set(reader.fieldnames) or timestamp_field is None:
                raise ValueError(f"{path}: expected IncidentId, SourceTable and timestamp columns")
            output_fields = [
                "timestamp",
                *(
                    name for name in reader.fieldnames
                    if name not in {"IncidentId", "SourceTable", "timestamp", "timestamp [UTC]"}
                ),
            ]
            for row_number, row in enumerate(reader, start=2):
                if row.get("IncidentId") != incident_id:
                    raise ValueError(f"{path}:{row_number}: incident ID does not match --incident-id")
                source = (row.get("SourceTable") or "").rsplit(".", 1)[-1]
                if source not in TABLES:
                    raise ValueError(f"{path}:{row_number}: unsupported source table {source!r}")
                if columns.setdefault(source, output_fields) != output_fields:
                    raise ValueError(f"{path}: slice columns do not match earlier {source} exports")
                output = {name: row.get(name, "") for name in output_fields}
                output["timestamp"] = _timestamp(row, path, row_number)
                grouped[source].append(output)

    directory.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for source, rows in sorted(grouped.items()):
        destination = directory / f"{source}.csv"
        with destination.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns[source], lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        counts[source] = len(rows)
    return counts
