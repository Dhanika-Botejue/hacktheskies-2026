"""Game server over HTTP/WebSocket: player tags and teams, the objectives, the shooting server's feed."""

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


def test_host_sets_objectives(client):
    with client.websocket_connect("/ws") as host:
        host.send_json({"role": "host"})
        next_msg(host, "map")
        host.send_json({"t": "points", "points": [
            {"id": "x", "kind": "base", "team": "Red", "name": "Mine", "x": 1.234, "y": 2, "r": 50},
            {"kind": "base", "team": "red", "x": 9, "y": 9},  # a second red base: the first one stays
            {"kind": "base", "team": "green", "x": 1, "y": 1},
            {"kind": "flag", "x": "bad", "y": 1},
            {"kind": "flag", "x": 5, "y": 6, "r": 0.1},
            {"id": "a", "name": "A", "x": 1, "y": 2},  # an old key position with no kind
        ]})
        points = next_msg(host, "map")["map"]["points"]
    assert points == [
        {"id": "base-red", "kind": "base", "team": "red", "name": "Red base", "x": 1.23, "y": 2.0, "r": 10.0},
        {"id": "flag", "kind": "flag", "name": "Flag", "x": 5.0, "y": 6.0, "r": 0.5},
    ]
    assert client.get("/api/map").json()["points"] == points


def test_watcher_gets_map_and_positions_without_being_a_player(client):
    with client.websocket_connect("/ws") as watcher, client.websocket_connect("/ws") as phone:
        watcher.send_json({"role": "watch"})
        assert "points" in next_msg(watcher, "map")["map"]
        phone.send_json({"role": "player", "id": "p1", "name": "Ana", "tag": 3, "team": "blue"})
        phone.send_json({"t": "calibrate", "x": 4, "y": 5, "mapHeading": 0})
        while True:
            players = next_msg(watcher, "state")["players"]
            if players and players[0]["calibrated"]:
                break
    assert [(p["id"], p["tag"]) for p in players] == [("p1", 3)]
    assert abs(players[0]["x"] - 4) < 1 and abs(players[0]["y"] - 5) < 1
    assert "particles" not in players[0]  # the light update phones get


def test_saving_the_map_keeps_objectives_unless_reset(client):
    with client.websocket_connect("/ws") as host:
        host.send_json({"role": "host"})
        next_msg(host, "map")
        host.send_json({"t": "points", "points": [{"kind": "flag", "x": 1, "y": 2}]})
        next_msg(host, "map")

    # The 2D host page saves walls with an old copy of the map: the points stay.
    stale = {**main.DEFAULT_MAP, "walls": [[0, 0, 1, 1]], "points": []}
    assert client.put("/api/map", json=stale).status_code == 200
    saved = client.get("/api/map").json()
    assert saved["walls"] == [[0, 0, 1, 1]]
    assert [p["id"] for p in saved["points"]] == ["flag"]

    # The viewer made a new scan the game map: the points no longer line up.
    assert client.put("/api/map", json={**main.DEFAULT_MAP, "pointsReset": True}).status_code == 200
    saved = client.get("/api/map").json()
    assert saved["points"] == []
    assert "pointsReset" not in saved


def test_bad_map_is_refused_and_the_old_one_kept(client):
    before = client.get("/api/map").json()
    assert client.put("/api/map", json={**before, "walls": [[1, 2, 3]]}).status_code == 400
    assert client.put("/api/map", json=[1, 2]).status_code == 400
    assert client.get("/api/map").json() == before


def test_a_malformed_message_is_skipped_not_fatal(client):
    with client.websocket_connect("/ws") as watcher, client.websocket_connect("/ws") as phone:
        watcher.send_json({"role": "watch"})
        phone.send_json({"role": "player", "id": "p1", "name": "Ana"})
        phone.send_json({"t": "pose"})
        phone.send_json({"t": "step", "heading": 0, "len": None})
        phone.send_json({"t": "calibrate", "x": 4, "y": 5, "mapHeading": 0})
        while True:
            players = next_msg(watcher, "state")["players"]
            if players and players[0]["calibrated"]:
                break


def test_a_silent_phone_stops_counting_as_connected(client, monkeypatch):
    monkeypatch.setattr(main, "SILENT_S", 0.3)
    with client.websocket_connect("/ws") as watcher, client.websocket_connect("/ws") as phone:
        watcher.send_json({"role": "watch"})
        phone.send_json({"role": "player", "id": "p1", "name": "Ana"})
        while True:  # e.g. its screen locked: the socket stays open but nothing comes from it
            players = next_msg(watcher, "state")["players"]
            if players and not players[0]["connected"]:
                break
        phone.send_json({"t": "heading", "gyro": 10})
        while True:
            players = next_msg(watcher, "state")["players"]
            if players[0]["connected"]:
                break


def test_removing_a_connected_player_hangs_up_on_them(client):
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect("/ws") as host, client.websocket_connect("/ws") as phone:
        host.send_json({"role": "host"})
        phone.send_json({"role": "player", "id": "p1", "name": "Ana"})
        while not next_msg(host, "state")["players"]:
            pass
        host.send_json({"t": "remove", "id": "p1"})
        with pytest.raises(WebSocketDisconnect):  # so the app reconnects and joins afresh
            for _ in range(200):
                phone.receive_json()
