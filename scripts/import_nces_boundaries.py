import argparse
import asyncio
import struct
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import delete, insert, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import AsyncSessionLocal
from app.models.database import NcesAttendanceBoundary, Property


BRIGHT_MLS_DEFAULT_STATES = {"PA", "MD", "VA", "NJ", "DE", "DC", "WV"}


def _dbf_fields(dbf_file) -> tuple[int, int, list[tuple[str, str, int]]]:
    header = dbf_file.read(32)
    _, _, _, _, record_count, header_len, record_len = struct.unpack("<BBBBIHH20x", header)
    fields = []
    while True:
        desc = dbf_file.read(32)
        if not desc or desc[0] == 0x0D:
            break
        name = desc[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        fields.append((name, chr(desc[11]), desc[16]))
    dbf_file.seek(header_len)
    return record_count, record_len, fields


def _read_dbf_record(dbf_file, record_len: int, fields: list[tuple[str, str, int]]) -> Optional[dict[str, str]]:
    record = dbf_file.read(record_len)
    if not record:
        return None
    if record[0:1] == b"*":
        return {}
    values = {}
    pos = 1
    for name, _field_type, length in fields:
        raw = record[pos:pos + length]
        pos += length
        values[name] = raw.decode("utf-8", errors="replace").strip()
    return values


def _read_shp_record(shp_file) -> Optional[bytes]:
    header = shp_file.read(8)
    if not header:
        return None
    if len(header) != 8:
        raise ValueError("Truncated shapefile record header")
    _record_number, content_len_words = struct.unpack(">ii", header)
    return shp_file.read(content_len_words * 2)


def _parse_polygon(content: bytes) -> Optional[dict]:
    if not content:
        return None
    shape_type = struct.unpack("<i", content[:4])[0]
    if shape_type == 0:
        return None
    if shape_type != 5:
        raise ValueError(f"Unsupported NCES shapefile shape type {shape_type}; expected Polygon (5)")

    xmin, ymin, xmax, ymax = struct.unpack("<dddd", content[4:36])
    num_parts, num_points = struct.unpack("<ii", content[36:44])
    parts_offset = 44
    points_offset = parts_offset + num_parts * 4
    parts = list(struct.unpack(f"<{num_parts}i", content[parts_offset:points_offset]))
    points = [
        list(struct.unpack("<dd", content[points_offset + i * 16:points_offset + (i + 1) * 16]))
        for i in range(num_points)
    ]

    rings = []
    for idx, start in enumerate(parts):
        end = parts[idx + 1] if idx + 1 < len(parts) else num_points
        ring = points[start:end]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) >= 4:
            rings.append(ring)

    if not rings:
        return None
    return {"bbox": (xmin, ymin, xmax, ymax), "geometry": rings}


def _find_zip_member(zip_file: zipfile.ZipFile, suffix: str) -> str:
    return next(name for name in zip_file.namelist() if name.lower().endswith(suffix.lower()))


async def _states_from_properties() -> set[str]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Property.state).where(Property.state.isnot(None)).distinct())).scalars().all()
    return {state.upper() for state in rows if state and len(state) == 2}


async def _replace_states(states: set[str]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(NcesAttendanceBoundary).where(NcesAttendanceBoundary.state.in_(states)))
        await db.commit()


async def _insert_batch(batch: list[dict]) -> None:
    if not batch:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(insert(NcesAttendanceBoundary), batch)
        await db.commit()


async def import_boundaries(zip_path: Path, states: set[str], replace: bool, batch_size: int = 250) -> int:
    if replace:
        await _replace_states(states)

    inserted = 0
    batch: list[dict] = []

    with zipfile.ZipFile(zip_path) as zf:
        dbf_name = _find_zip_member(zf, ".dbf")
        shp_name = _find_zip_member(zf, ".shp")
        with zf.open(dbf_name) as dbf_file, zf.open(shp_name) as shp_file:
            shp_file.read(100)
            record_count, record_len, fields = _dbf_fields(dbf_file)

            for record_index in range(record_count):
                attrs = _read_dbf_record(dbf_file, record_len, fields)
                shp_content = _read_shp_record(shp_file)
                if attrs is None or shp_content is None:
                    break
                if not attrs:
                    continue

                state = (attrs.get("stAbbrev") or "").upper()
                if state not in states:
                    continue

                polygon = _parse_polygon(shp_content)
                if polygon is None:
                    continue

                xmin, ymin, xmax, ymax = polygon["bbox"]
                batch.append({
                    "id": str(uuid.uuid4()),
                    "school_id": attrs.get("ncessch") or "",
                    "school_name": attrs.get("schnam") or "",
                    "lea_id": attrs.get("leaid") or None,
                    "state": state,
                    "grade_low": attrs.get("gslo") or None,
                    "grade_high": attrs.get("gshi") or None,
                    "level": attrs.get("level") or None,
                    "source_name": attrs.get("SrcName") or None,
                    "defacto": attrs.get("defacto") or None,
                    "open_enroll": attrs.get("openEnroll") or None,
                    "multi_boundary": attrs.get("MultiBdy") or None,
                    "bbox_xmin": xmin,
                    "bbox_ymin": ymin,
                    "bbox_xmax": xmax,
                    "bbox_ymax": ymax,
                    "geometry": polygon["geometry"],
                })

                if len(batch) >= batch_size:
                    await _insert_batch(batch)
                    inserted += len(batch)
                    print(f"Imported {inserted} NCES boundaries...", flush=True)
                    batch.clear()

            if batch:
                await _insert_batch(batch)
                inserted += len(batch)

    return inserted


def _parse_states(raw_states: Optional[str]) -> set[str]:
    if not raw_states:
        return set()
    return {state.strip().upper() for state in raw_states.split(",") if state.strip()}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import NCES SABS school attendance boundaries from the official shapefile ZIP.")
    parser.add_argument("zip_path", type=Path, help="Path to SABS_1516.zip")
    parser.add_argument("--states", help="Comma-separated state abbreviations to import, e.g. PA,MD,NJ")
    parser.add_argument("--all-states", action="store_true", help="Import every state in the shapefile")
    parser.add_argument("--replace", action="store_true", help="Delete existing imported rows for selected states before importing")
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()

    if args.all_states:
        states = {chr(a) + chr(b) for a in range(ord("A"), ord("Z") + 1) for b in range(ord("A"), ord("Z") + 1)}
    else:
        states = _parse_states(args.states)
        if not states:
            states = await _states_from_properties()
        states = {state for state in states if state in BRIGHT_MLS_DEFAULT_STATES}

    if not states:
        raise SystemExit("No states selected. Pass --states PA,MD or --all-states.")

    print(f"Importing NCES boundaries for: {', '.join(sorted(states))}")
    inserted = await import_boundaries(args.zip_path, states=states, replace=args.replace, batch_size=args.batch_size)
    print(f"Imported {inserted} NCES attendance boundaries.")


if __name__ == "__main__":
    asyncio.run(main())
