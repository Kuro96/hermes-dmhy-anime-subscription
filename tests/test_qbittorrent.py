from urllib.parse import parse_qs

from hermes_dmhy_anime_subscription.config import QbittorrentConfig
from hermes_dmhy_anime_subscription.models import FeedItem, ReleaseCandidate, SubscriptionRule
from hermes_dmhy_anime_subscription.qbittorrent import (
    QbittorrentClient,
    QbittorrentHttpRequest,
    QbittorrentHttpResponse,
    plan_qbittorrent_submission,
)


class MockTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def send(self, request: QbittorrentHttpRequest) -> QbittorrentHttpResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def test_login_runs_before_submit_when_credentials_are_enabled():
    transport = MockTransport(
        [
            QbittorrentHttpResponse(status=200, body="Ok."),
            QbittorrentHttpResponse(status=200, body="Ok."),
        ]
    )

    result = QbittorrentClient(_config(), username="user", password="fixture-pass", transport=transport).submit(_candidate())

    assert result.success is True
    assert [request.url for request in transport.requests] == [
        "http://127.0.0.1:8080/api/v2/auth/login",
        "http://127.0.0.1:8080/api/v2/torrents/add",
    ]
    login_payload = parse_qs(transport.requests[0].data.decode("utf-8"))
    assert login_payload == {"username": ["user"], "password": ["fixture-pass"]}


def test_add_request_includes_magnet_category_tags_and_rule_save_path():
    transport = MockTransport([QbittorrentHttpResponse(status=200, body="Ok.")])
    rule = SubscriptionRule(name="example-rule", category="rule-anime", save_path="/downloads/rule")

    result = QbittorrentClient(_config(), transport=transport).submit(_candidate(), rule=rule)

    add_payload = parse_qs(transport.requests[0].data.decode("utf-8"))
    assert result.status == "submitted"
    assert add_payload["urls"] == ["magnet:?xt=urn:btih:ABC123"]
    assert add_payload["category"] == ["rule-anime"]
    assert add_payload["tags"] == ["dmhy,subscription"]
    assert add_payload["savepath"] == ["/downloads/rule"]


def test_duplicate_response_is_idempotent_success():
    transport = MockTransport([QbittorrentHttpResponse(status=409, body="Torrent already in the transfer list")])

    result = QbittorrentClient(_config(), transport=transport).submit(_candidate())

    assert result.success is True
    assert result.duplicate is True
    assert result.status == "duplicate"
    assert result.retryable is False


def test_timeout_is_retryable_failure_not_processed_success():
    transport = MockTransport(error=TimeoutError("timed out"))

    result = QbittorrentClient(_config(), transport=transport).submit(_candidate())

    assert result.success is False
    assert result.status == "failed"
    assert result.retryable is True
    assert result.duplicate is False
    assert result.error is not None
    assert result.error.kind == "transport"


def test_dry_run_plans_submission_without_http_calls():
    transport = MockTransport([QbittorrentHttpResponse(status=200, body="Ok.")])

    result = QbittorrentClient(_config(), transport=transport).submit(_candidate(), dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert result.plan.payload() == {
        "urls": "magnet:?xt=urn:btih:ABC123",
        "category": "anime",
        "tags": "dmhy,subscription",
        "savepath": "/downloads/anime",
    }
    assert transport.requests == []


def test_planner_uses_torrent_url_when_magnet_is_absent():
    candidate = ReleaseCandidate(
        feed_item=FeedItem(title="Torrent URL", link="https://dmhy.example/item", torrent_url="https://dmhy.example/file.torrent"),
        rule_name="url-rule",
        title="Torrent URL",
    )

    plan = plan_qbittorrent_submission(candidate, _config())

    assert plan.source == "https://dmhy.example/file.torrent"
    assert plan.payload()["urls"] == "https://dmhy.example/file.torrent"



def test_list_torrents_logs_in_and_filters_configured_category():
    transport = MockTransport(
        [
            QbittorrentHttpResponse(status=200, body="Ok."),
            QbittorrentHttpResponse(
                status=200,
                body='[{"hash":"ABCDEF","name":"Example.mkv","state":"uploading","progress":1,"save_path":"/downloads/anime","content_path":"/downloads/anime/Example.mkv","completion_on":123}]',
            ),
        ]
    )

    torrents = QbittorrentClient(_config_with_auth(), username="user", password="fixture-pass", transport=transport).list_torrents()

    assert [request.url for request in transport.requests] == [
        "http://127.0.0.1:8080/api/v2/auth/login",
        "http://127.0.0.1:8080/api/v2/torrents/info?category=anime",
    ]
    assert transport.requests[1].method == "GET"
    assert torrents[0].torrent_hash == "abcdef"
    assert torrents[0].name == "Example.mkv"
    assert torrents[0].progress == 1.0
    assert torrents[0].content_path == "/downloads/anime/Example.mkv"


def test_list_torrents_all_categories_omits_category_filter():
    transport = MockTransport(
        [
            QbittorrentHttpResponse(status=200, body="Ok."),
            QbittorrentHttpResponse(status=200, body="[]"),
        ]
    )

    QbittorrentClient(_config_with_auth(), username="user", password="fixture-pass", transport=transport).list_torrents(all_categories=True)

    assert [request.url for request in transport.requests] == [
        "http://127.0.0.1:8080/api/v2/auth/login",
        "http://127.0.0.1:8080/api/v2/torrents/info",
    ]


def test_list_torrents_preserves_empty_category_filter_for_uncategorized():
    transport = MockTransport(
        [
            QbittorrentHttpResponse(status=200, body="Ok."),
            QbittorrentHttpResponse(status=200, body="[]"),
        ]
    )

    QbittorrentClient(_config_with_auth(), username="user", password="fixture-pass", transport=transport).list_torrents(category="")

    assert [request.url for request in transport.requests] == [
        "http://127.0.0.1:8080/api/v2/auth/login",
        "http://127.0.0.1:8080/api/v2/torrents/info?category=",
    ]


def _config() -> QbittorrentConfig:
    return QbittorrentConfig(
        endpoint="http://127.0.0.1:8080",
        category="anime",
        tags=("dmhy", "subscription"),
        save_path="/downloads/anime",
    )


def _config_with_auth() -> QbittorrentConfig:
    return QbittorrentConfig(
        endpoint="http://127.0.0.1:8080",
        category="anime",
        tags=("dmhy", "subscription"),
        save_path="/downloads/anime",
        username_env="QBITTORRENT_USERNAME",
        password_env="QBITTORRENT_PASSWORD",
    )


def _candidate() -> ReleaseCandidate:
    return ReleaseCandidate(
        feed_item=FeedItem(title="Example Anime", link="https://dmhy.example/item", magnet_uri="magnet:?xt=urn:btih:ABC123"),
        rule_name="example-rule",
        title="Example Anime",
    )
