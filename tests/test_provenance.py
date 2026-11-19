"""Boundary-provenance API: per-edge way/direction tracing and durability."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import Database
from app.geometry import build_polygon
from app.main import app
from app.models import RevisionIn

from .factories import (
    INNER_NODES,
    INNER_WAY,
    SQUARE_NODES,
    SQUARE_WAY,
    node,
    revision,
    way,
)

# Split square 1 -> 2 -> 3 -> 4 -> 1, with ways 12 and 13 submitted reversed.
SPLIT_WAYS = [
    way(11, [2, 3]),
    way(12, [1, 4]),
    way(13, [4, 3]),
    way(14, [1, 2]),
]

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def post(client: TestClient, relation_id: int, payload: dict):
    return client.post(f"/relations/{relation_id}/revisions", json=payload)


def source(client: TestClient, relation_id: int, revision_number: int) -> dict:
    resp = client.get(
        f"/relations/{relation_id}/revisions/{revision_number}/boundary-source"
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def edge_tuples(body: dict, ring_index: int = 0) -> list[tuple]:
    ring = body["rings"][ring_index]
    return [
        (e["from_node"], e["to_node"], e["way_id"], e["way_forward"])
        for e in sorted(ring["edges"], key=lambda e: e["edge_index"])
    ]


# ------------------------------------------------------------------- happy path
def test_single_way_edges_map_to_that_way_and_follow_input_direction(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    body = source(client, relation_id, 1)

    assert body["relation_id"] == relation_id
    assert body["revision"] == 1
    assert body["source"] == "revision"
    assert body["geometry"]["type"] == "Polygon"
    assert len(body["rings"]) == 1
    ring = body["rings"][0]
    assert ring["ring_index"] == 0 and ring["role"] == "outer"
    assert [e["edge_index"] for e in ring["edges"]] == [0, 1, 2, 3]
    assert edge_tuples(body) == [
        (1, 2, 10, True),
        (2, 3, 10, True),
        (3, 4, 10, True),
        (4, 1, 10, True),
    ]


def test_segmented_ways_keep_per_edge_source_and_reversed_flag(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, SPLIT_WAYS))
    body = source(client, relation_id, 1)
    # Attribution follows the stitch, not the submission order.
    assert edge_tuples(body) == [
        (2, 3, 11, True),
        (3, 4, 13, False),  # way 13 is [4, 3] -> traversed backwards
        (4, 1, 12, False),  # way 12 is [1, 4] -> traversed backwards
        (1, 2, 14, True),
    ]


def test_reordering_input_ways_keeps_each_edge_attributed(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, SPLIT_WAYS))
    shuffled = [SPLIT_WAYS[i] for i in (3, 1, 2, 0)]  # walk starts at way 14
    post(client, relation_id, revision(2, SQUARE_NODES, shuffled))

    first = source(client, relation_id, 1)
    second = source(client, relation_id, 2)
    # Same edges with the same source and direction, even though the ring
    # is walked from another way and therefore rotated.
    assert set(edge_tuples(first)) == set(edge_tuples(second))
    assert {t[2] for t in edge_tuples(first)} == {11, 12, 13, 14}
    assert sorted(t[2] for t in edge_tuples(second)) == [11, 12, 13, 14]


def test_edges_line_up_one_to_one_with_geojson_ring_coordinates(
    client: TestClient, relation_id: int
):
    payload = revision(1, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY])
    post(client, relation_id, payload)
    body = source(client, relation_id, 1)

    nodes = {n["id"]: [n["lon"], n["lat"]] for n in payload["nodes"]}
    coords = body["geometry"]["coordinates"]
    assert len(body["rings"]) == len(coords) == 2
    for ring, ring_coords in zip(body["rings"], coords):
        edges = sorted(ring["edges"], key=lambda e: e["edge_index"])
        assert [e["edge_index"] for e in edges] == list(range(len(ring_coords) - 1))
        for edge in edges:
            start = ring_coords[edge["edge_index"]]
            end = ring_coords[edge["edge_index"] + 1]
            assert list(start) == nodes[edge["from_node"]], (start, edge)
            assert list(end) == nodes[edge["to_node"]], (end, edge)


def test_inner_ring_listed_after_outer_with_its_role(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY]))
    body = source(client, relation_id, 1)
    assert [(r["ring_index"], r["role"]) for r in body["rings"]] == [
        (0, "outer"),
        (1, "inner"),
    ]
    assert edge_tuples(body, 1) == [
        (5, 6, 20, True),
        (6, 7, 20, True),
        (7, 8, 20, True),
        (8, 5, 20, True),
    ]


def test_unordered_example_file_attributes_each_edge(client: TestClient, relation_id: int):
    payload = json.loads(
        (EXAMPLES / "rev4_valid_unordered_ways.json").read_text(encoding="utf-8")
    )
    assert post(client, relation_id, payload).status_code == 200

    edges = source(client, relation_id, payload["revision"])["rings"][0]["edges"]
    by_way = {e["way_id"]: e for e in edges}
    assert set(by_way) == {11, 12, 13, 14}
    assert (by_way[12]["from_node"], by_way[12]["to_node"], by_way[12]["way_forward"]) == (
        4,
        1,
        False,
    )
    assert (by_way[13]["from_node"], by_way[13]["to_node"], by_way[13]["way_forward"]) == (
        3,
        4,
        False,
    )
    assert by_way[11]["way_forward"] is True
    assert by_way[14]["way_forward"] is True


def test_multi_node_reversed_way_reports_unambiguous_direction(
    client: TestClient, relation_id: int
):
    # Way 11 covers 4 -> 3 -> 2 in the input but the stitch must traverse it
    # as 2 -> 3 -> 4.  Direction cannot be inferred from coordinates (a
    # reversed walk produces the same points), only from the bookkeeping.
    nodes = [node(1, 0.0, 0.0), node(2, 1.0, 0.0), node(3, 1.0, 1.0), node(4, 0.0, 1.0)]
    ways = [way(12, [4, 1]), way(13, [1, 2]), way(11, [4, 3, 2])]
    assert post(client, relation_id, revision(1, nodes, ways)).status_code == 200

    body = source(client, relation_id, 1)
    eleven = [e for e in body["rings"][0]["edges"] if e["way_id"] == 11]
    # The ring is walked as 4 -> 1 (way 12), 1 -> 2 (way 13), 2 -> 3 -> 4
    # (way 11 traversed backwards).
    assert [(e["edge_index"], e["from_node"], e["to_node"], e["way_forward"]) for e in eleven] == [
        (2, 2, 3, False),
        (3, 3, 4, False),
    ]


# ------------------------------------------------------ invalid / current state
def test_invalid_revision_has_no_source_but_old_valid_source_remains(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    bad = post(
        client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])])
    )
    assert bad.json()["status"] == "invalid"

    resp = client.get(f"/relations/{relation_id}/revisions/2/boundary-source")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["last_valid_revision"] == 1
    assert body["error"]["code"] == "missing_nodes"
    assert "rings" not in body

    # Current face and its source stop being served ...
    cur = client.get(f"/relations/{relation_id}/current/boundary-source")
    assert cur.status_code == 409
    assert cur.json()["last_valid_revision"] == 1

    # ... while the previous valid source stays queryable by revision number.
    old = source(client, relation_id, 1)
    assert old["revision"] == 1 and len(old["rings"][0]["edges"]) == 4


def test_current_boundary_source_tracks_current_served_face(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    current = client.get(f"/relations/{relation_id}/current/boundary-source")
    assert current.status_code == 200
    body = current.json()
    assert body["source"] == "current" and body["revision"] == 1

    post(client, relation_id, revision(3, SQUARE_NODES, SPLIT_WAYS))
    current = client.get(f"/relations/{relation_id}/current/boundary-source").json()
    assert current["revision"] == 3
    assert {e["way_id"] for e in current["rings"][0]["edges"]} == {11, 12, 13, 14}


def test_boundary_source_not_found_paths(client: TestClient, relation_id: int):
    for path in ("revisions/5/boundary-source", "current/boundary-source"):
        resp = client.get(f"/relations/{relation_id}/{path}")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "not_found"


def test_boundary_source_paths_appear_in_openapi(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert "/relations/{relation_id}/revisions/{revision}/boundary-source" in schema["paths"]
    assert "/relations/{relation_id}/current/boundary-source" in schema["paths"]
    assert schema["info"]["title"]


# ------------------------------------------------------------------- durability
def test_source_survives_restart(db_path: str, relation_id: int):
    db = Database(db_path)
    app.state.db = db
    with TestClient(app) as client:
        app.state.db = db
        post(client, relation_id, revision(1, SQUARE_NODES, SPLIT_WAYS))
        before = source(client, relation_id, 1)

    # Simulate a process restart: a brand new Database over the same file.
    restarted = Database(db_path)
    app.state.db = restarted
    plain_client = TestClient(app)  # no lifespan needed, state already set
    try:
        row = restarted.get_revision(relation_id, 1)
        expected = [
            {"role": r["role"], "edges": r["edges"]} for r in before["rings"]
        ]
        assert row["provenance"]["rings"] == expected
        after = source(plain_client, relation_id, 1)
        current = plain_client.get(
            f"/relations/{relation_id}/current/boundary-source"
        ).json()
    finally:
        plain_client.close()
    assert after["rings"] == before["rings"]
    assert current["revision"] == 1 and current["rings"] == before["rings"]


def test_legacy_schema_is_migrated_with_provenance_column(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE revisions (
            relation_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
            input_json TEXT NOT NULL,
            geometry_json TEXT,
            error_code TEXT,
            error_message TEXT,
            error_way_ids_json TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (relation_id, revision)
        )
        """
    )
    conn.commit()
    conn.close()

    Database(db_path)  # migrates in place
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(revisions)")}
    finally:
        conn.close()
    assert "provenance_json" in columns


def test_geometry_and_provenance_are_written_together_or_not_at_all(
    db_path: str, relation_id: int
):
    db = Database(db_path)
    good = revision(1, SQUARE_NODES, [SQUARE_WAY])
    geom, provenance = build_polygon(RevisionIn(**good))

    # Half-pairs are refused instead of storing a valid revision without source.
    for geometry, provenance_arg in ((geom, None), (None, provenance), (None, None)):
        try:
            db.save_revision(relation_id, 1, "valid", good, geometry, None, provenance_arg)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected pairing error")
    assert db.list_revisions(relation_id) == []

    # A storage failure rolls back the row, geometry and source together.
    try:
        db.save_revision(relation_id, 2, "bogus-status", good, geom, None, provenance)
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected storage failure")
    assert db.get_revision(relation_id, 2) is None
    assert db.list_revisions(relation_id) == []

    db.save_revision(relation_id, 1, "valid", good, geom, None, provenance)
    row = db.get_revision(relation_id, 1)
    assert row["geometry"] is not None and row["provenance"] is not None


def test_revisions_never_mix_sources(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, SQUARE_NODES, SPLIT_WAYS))

    first = source(client, relation_id, 1)
    second = source(client, relation_id, 2)
    assert {e["way_id"] for e in first["rings"][0]["edges"]} == {10}
    assert {e["way_id"] for e in second["rings"][0]["edges"]} == {11, 12, 13, 14}
    assert first["geometry"] != second["geometry"]
    for body in (first, second):
        for ring, ring_coords in zip(body["rings"], body["geometry"]["coordinates"]):
            # Edge counts belong to this revision's own ring length only.
            assert len(ring["edges"]) == len(ring_coords) - 1


def test_concurrent_uploads_and_source_reads_never_see_torn_revision(
    client: TestClient, db: Database, relation_id: int
):
    """Uploads and source reads interleaved: every readable revision is
    complete (geometry + matching edge list), never a half-written row."""
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    stop = threading.Event()
    problems: list[str] = []
    seen: dict[int, dict] = {}

    # Separate client -> separate anyio portal, mirroring two real HTTP
    # clients (see test_api.py for the same pattern).
    reader_client = TestClient(app)
    reader_client.__enter__()
    app.state.db = db

    def reader() -> None:
        while not stop.is_set():
            listing = reader_client.get(
                f"/relations/{relation_id}/revisions"
            ).json()["revisions"]
            for item in listing:
                rev = item["revision"]
                resp = reader_client.get(
                    f"/relations/{relation_id}/revisions/{rev}/boundary-source"
                )
                if resp.status_code != 200:
                    problems.append(f"revision {rev}: HTTP {resp.status_code}")
                    continue
                body = resp.json()
                coords = body["geometry"]["coordinates"]
                if len(body["rings"]) != len(coords):
                    problems.append(f"revision {rev}: ring/coord mismatch")
                    continue
                for ring, ring_coords in zip(body["rings"], coords):
                    if len(ring["edges"]) != len(ring_coords) - 1:
                        problems.append(f"revision {rev}: torn edge list")
                if rev in seen and seen[rev] != body:
                    problems.append(f"revision {rev}: changed between reads")
                seen[rev] = body

    t = threading.Thread(target=reader)
    t.start()
    try:
        for rev in range(2, 12):
            payload = (
                revision(rev, SQUARE_NODES, SPLIT_WAYS)
                if rev % 2
                else revision(rev, SQUARE_NODES, [SQUARE_WAY])
            )
            assert post(client, relation_id, payload).status_code == 200
        stop.set()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        stop.set()
        reader_client.__exit__(None, None, None)
        app.state.db = db
    assert problems == []
    # Every stored valid revision is reachable and complete after the storm.
    for rev in range(1, 12):
        body = source(client, relation_id, rev)
        assert len(body["rings"]) == len(body["geometry"]["coordinates"])
