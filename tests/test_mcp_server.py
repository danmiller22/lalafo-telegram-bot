from __future__ import annotations

import pytest

from app.lalafo.mcp_server import _lalafo_url


def test_lalafo_url_accepts_expected_domain() -> None:
    assert _lalafo_url("https://lalafo.kg/bishkek") == "https://lalafo.kg/bishkek"


@pytest.mark.parametrize(
    "url",
    [
        "http://lalafo.kg/bishkek",
        "https://example.com/",
        "https://lalafo.kg.example.com/",
    ],
)
def test_lalafo_url_rejects_unsafe_domains(url: str) -> None:
    with pytest.raises(ValueError):
        _lalafo_url(url)
