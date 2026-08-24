from app.lalafo.client import LalafoClient


SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/2-bedrooms/studio/owner/"
    "real-estate-agency/bez-podseleniya?price[to]=35000"
)


def test_search_params_preserve_configured_filters():
    params = dict(LalafoClient._search_params(SEARCH_URL, 3))
    assert params["category_id"] == "2044"
    assert params["city_id"] == "103184"
    assert params["page"] == "3"
    assert params["price[to]"] == "35000"
    assert set(params[key] for key in params if key.startswith("parameters[69]")) == {
        "15496",
        "2773",
        "2774",
    }
    assert set(params[key] for key in params if key.startswith("parameters[2149]")) == {
        "42340",
        "19057",
    }
    assert params["parameters[946][0]"] == "81537"
