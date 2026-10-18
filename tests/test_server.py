"""Game server over HTTP/WebSocket: player tags and teams, key positions."""

import pytest
from fastapi.testclient import TestClient

from server import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MAP_FILE", tmp_path / "map.json")
    monkeypatch.setattr(main.game, "map", dict(main.DEFAULT_MAP))
    monkeypatch.setattr(main.game, "players", {})
    with TestClient(main.app) as c:
        yield c


def next_msg(ws, kind):
    for _ in range(200):
        msg = ws.receive_json()
        if msg.get("t") == kind:
            return msg
    raise AssertionError(f"no {kind} message")


def test_player_tag_and_team_reach_the_host(client):
    with client.websocket_connect("/ws") as host, client.websocket_connect("/ws") as phone:
        host.send_json({"role": "host"})
        phone.send_json({"role": "player", "id": "p1", "name": "Ana", "tag": "7", "team": "Red"})
        while True:
            players = next_msg(host, "state")["players"]
            if players:
                break
        assert players[0]["tag"] == 7
        assert players[0]["team"] == "red"


@pytest.mark.parametrize("tag,team", [(99, "green"), (None, None), ("x", 3)])
def test_bad_tag_or_team_is_dropped(client, tag, team):
    with client.websocket_connect("/ws") as host, client.websocket_connect("/ws") as phone:
        host.send_json({"role": "host"})
        phone.send_json({"role": "player", "id": "p1", "name": "Ana", "tag": tag, "team": team})
        while True:
            players = next_msg(host, "state")["players"]
            if players:
                break
        assert players[0]["tag"] is None
        assert players[0]["team"] is None


def test_host_sets_key_points(client):
    with client.websocket_connect("/ws") as host:
        host.send_json({"role": "host"})
        next_msg(host, "map")
        host.send_json({"t": "points", "points": [
            {"id": "a", "name": "A", "x": 1.234, "y": 2, "r": 50},
            {"id": "b", "x": "bad"},
        ]})
        points = next_msg(host, "map")["map"]["points"]
    assert points == [{"id": "a", "name": "A", "x": 1.23, "y": 2.0, "r": 10.0}]
    assert client.get("/api/map").json()["points"] == points


def test_saving_the_map_keeps_key_points_unless_reset(client):
    with client.websocket_connect("/ws") as host:
        host.send_json({"role": "host"})
        next_msg(host, "map")
        host.send_json({"t": "points", "points": [{"id": "a", "name": "A", "x": 1, "y": 2}]})
        next_msg(host, "map")

    # The 2D host page saves walls with an old copy of the map: the points stay.
    stale = {**main.DEFAULT_MAP, "walls": [[0, 0, 1, 1]], "points": []}
    assert client.put("/api/map", json=stale).status_code == 200
    saved = client.get("/api/map").json()
    assert saved["walls"] == [[0, 0, 1, 1]]
    assert [p["id"] for p in saved["points"]] == ["a"]

    # The viewer made a new scan the game map: the points no longer line up.
    assert client.put("/api/map", json={**main.DEFAULT_MAP, "pointsReset": True}).status_code == 200
    saved = client.get("/api/map").json()
    assert saved["points"] == []
    assert "pointsReset" not in saved
