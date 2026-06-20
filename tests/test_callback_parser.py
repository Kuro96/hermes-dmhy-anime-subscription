import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hermes_dmhy_anime_subscription.callback_parser import CallbackEpisodeParser
from hermes_dmhy_anime_subscription.config import OrganizerEpisodeParserConfig


def test_callback_episode_parser_success_posts_privacy_minimized_payload(tmp_path, monkeypatch):
    server, bodies = _server([{"series_title": "Example OVA", "season": 1, "episode": 13, "confidence": 0.95}])
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse?token=secret")
    source = tmp_path / "private" / "downloads" / "[Subs] Example OVA [1080p].mkv"
    parser = CallbackEpisodeParser(
        OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8),
        source_path=source,
        metadata={
            "rule_name": "example-rule",
            "bangumi_subject_id": 12345,
            "release_group": "Subs",
            "quality": "1080p",
            "category": "anime",
            "source_path": str(source),
            "content_path": "/private/content",
            "save_path": "/private/save",
            "original_content_path": "/private/original",
            "torrent_hash": "ABCDEF",
            "job_id": "job-secret",
            "webhook_url": "https://example.invalid/hook",
            "token": "secret-token",
            "username": "alice",
            "chat_id": "-100123",
        },
    )
    try:
        result = parser("[Subs] Example OVA [1080p]")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is not None
    assert result.series_title == "Example OVA"
    assert result.episode == 13
    posted = bodies[0]
    assert posted["text"] == "[Subs] Example OVA [1080p]"
    assert posted["source_name"] == source.name
    assert posted["safe_context"] == {
        "rule_name": "example-rule",
        "bangumi_subject_id": 12345,
        "release_group": "Subs",
        "quality": "1080p",
        "category": "anime",
    }
    serialized = json.dumps(posted, sort_keys=True)
    assert str(source) not in serialized
    for forbidden in (
        "content_path",
        "save_path",
        "original_content_path",
        "torrent_hash",
        "job_id",
        "webhook_url",
        "secret-token",
        "alice",
        "chat_id",
    ):
        assert forbidden not in serialized


def test_callback_episode_parser_strips_windows_style_source_paths(monkeypatch):
    server, bodies = _server([{"series_title": "Example OVA", "episode": 13, "confidence": 0.95}])
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse")
    parser = CallbackEpisodeParser(
        OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8),
        source_path=r"C:\Users\alice\Downloads\Example OVA.mkv",
    )
    try:
        result = parser("Example OVA")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is not None
    assert bodies[0]["source_name"] == "Example OVA.mkv"
    serialized = json.dumps(bodies[0], sort_keys=True)
    assert "alice" not in serialized
    assert "Downloads" not in serialized
    assert "C:" not in serialized



def test_callback_episode_parser_filters_path_like_safe_context_values(monkeypatch):
    server, bodies = _server([{"series_title": "Example OVA", "episode": 13, "confidence": 0.95}])
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse")
    parser = CallbackEpisodeParser(
        OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8),
        metadata={
            "rule_name": "/home/alice/secret-rule",
            "release_group": r"C:\Users\alice",
            "quality": "1080p",
            "category": "anime",
            "bangumi_subject_id": 12345,
        },
    )
    try:
        result = parser("Example OVA")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is not None
    assert bodies[0]["safe_context"] == {"quality": "1080p", "category": "anime", "bangumi_subject_id": 12345}
    serialized = json.dumps(bodies[0], sort_keys=True)
    assert "alice" not in serialized
    assert "/home" not in serialized
    assert "C:" not in serialized



def test_callback_episode_parser_low_confidence_returns_none(monkeypatch):
    server, _bodies = _server([{"series_title": "Example OVA", "episode": 13, "confidence": 0.4}])
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse")
    parser = CallbackEpisodeParser(OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8))
    try:
        result = parser("[Subs] Example OVA [1080p]")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is None


def test_callback_episode_parser_error_returns_none(monkeypatch):
    server, _bodies = _server([{"error": "boom"}], status=500)
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse")
    parser = CallbackEpisodeParser(OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8))
    try:
        result = parser("[Subs] Example OVA [1080p]")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is None


def test_callback_episode_parser_invalid_response_returns_none(monkeypatch):
    server, _bodies = _server([{"series_title": "Example OVA", "episode": 0, "confidence": 0.95}])
    thread = _serve(server)
    monkeypatch.setenv("HERMES_EPISODE_PARSER_URL", f"http://127.0.0.1:{server.server_port}/parse")
    parser = CallbackEpisodeParser(OrganizerEpisodeParserConfig("callback", "HERMES_EPISODE_PARSER_URL", 2.0, 0.8))
    try:
        result = parser("[Subs] Example OVA [1080p]")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result is None


def _server(responses, status=200):
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
            response = responses.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        def log_message(self, format, *args):
            return None

    return HTTPServer(("127.0.0.1", 0), Handler), bodies


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
