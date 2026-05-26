import json
from urllib.error import URLError

from hermes_dmhy_anime_subscription.bangumi import lookup_chinese_title


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
