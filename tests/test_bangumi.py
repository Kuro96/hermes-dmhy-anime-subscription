import json
from urllib.error import URLError

from hermes_dmhy_anime_subscription.bangumi import fetch_subject_cover_url, fetch_subject_main_episodes, lookup_chinese_title


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_lookup_chinese_title_posts_search_with_timeout_and_user_agent():
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _Response({"data": [{"name": "Sousou no Frieren", "name_cn": "葬送的芙莉莲"}]})

    assert lookup_chinese_title("Frieren Beyond Journeys End", opener=opener, timeout=4.5) == "葬送的芙莉莲"

    request, timeout = calls[0]
    assert request.full_url == "https://api.bgm.tv/v0/search/subjects"
    assert timeout == 4.5
    assert request.headers["User-agent"].startswith("hermes-dmhy-anime-subscription/")
    assert json.loads(request.data.decode("utf-8"))["keyword"] == "Frieren Beyond Journeys End"


def test_lookup_chinese_title_returns_none_on_network_or_empty_result():
    def opener(request, *, timeout):
        raise URLError("offline")

    assert lookup_chinese_title("Frieren Beyond Journeys End", opener=opener) is None


def test_fetch_subject_main_episodes_uses_v0_subject_and_main_episode_endpoints():
    calls = []
    responses = [
        {"id": 12345, "eps": 2},
        {"data": [{"id": 1, "type": 0, "ep": 1}, {"id": 2, "type": 0, "ep": 2}], "total": 2},
    ]

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _Response(responses.pop(0))

    result = fetch_subject_main_episodes(12345, opener=opener, timeout=4.5)

    assert result.subject_id == 12345
    assert result.eps == 2
    assert result.main_episode_numbers == (1, 2)
    assert calls[0][0].full_url == "https://api.bgm.tv/v0/subjects/12345"
    assert calls[1][0].full_url == "https://api.bgm.tv/v0/episodes?subject_id=12345&type=0&limit=200&offset=0"
    assert calls[0][1] == 4.5
    assert calls[0][0].headers["User-agent"].startswith("hermes-dmhy-anime-subscription/")


def test_fetch_subject_cover_url_extracts_v0_subject_image_in_preferred_order():
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _Response(
            {
                "images": {
                    "large": "https://img.example.invalid/large.jpg",
                    "common": "https://img.example.invalid/common.jpg",
                    "grid": "https://img.example.invalid/grid.jpg",
                }
            }
        )

    result = fetch_subject_cover_url(12345, opener=opener, timeout=4.5)

    assert result == "https://img.example.invalid/common.jpg"
    assert calls[0][0].full_url == "https://api.bgm.tv/v0/subjects/12345"
    assert calls[0][1] == 4.5
