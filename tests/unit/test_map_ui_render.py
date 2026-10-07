'''地图工具族 UI 卡片渲染回归测试（POI / geocode 卡片）。'''

import pytest

import token_budget


class _FakeEncoding:
    """轻量级 tiktoken.Encoding 替身，避免测试依赖真实编码文件下载。"""

    def encode(self, text: str, disallowed_special=()) -> list:
        return list(text.encode("utf-8"))

    def decode(self, tokens: list) -> str:
        return bytes(tokens).decode("utf-8", errors="ignore")


@pytest.fixture(autouse=True)
def _stub_tiktoken_encoding(monkeypatch):
    token_budget._get_encoding.cache_clear()
    monkeypatch.setattr(token_budget, "_get_encoding", lambda name: _FakeEncoding())
    yield


from tool_ui_render import (  # noqa: E402  (需在 stub 生效后导入)
    _render_poi_cards,
    _render_map_location_card,
    _render_map_payload,
)


def _sample_poi(**overrides) -> dict:
    base = {
        "id": "B0FFF5UV26",
        "name": "肯德基(三里屯店)",
        "address": "三里屯路11号院1号楼",
        "location": "116.48,39.99",
        "tel": "010-12345678",
        "distance": "350",
        "type": "餐饮服务;中餐厅;川菜",
        "biz_ext": {
            "cost": "35.00", "rating": "4.6", "business_area": "三里屯商圈",
            "tag": "烤鱼,辣子鸡", "opentime2": "周一至周日 10:00-22:00"
        },
    }
    base.update(overrides)
    return base


def test_poi_cards_no_longer_use_per_item_details():
    out = _render_poi_cards({"pois": [_sample_poi(), _sample_poi(name="星巴克")]})
    assert out is not None
    assert "<details" not in out
    assert "<summary" not in out


def test_poi_cards_show_decision_fields():
    out = _render_poi_cards({"pois": [_sample_poi()]})
    assert "010-12345678" in out
    assert "4.6" in out
    assert "35.00" in out
    assert "餐饮服务/川菜" in out
    assert "周一至周日 10:00-22:00" in out
    assert "三里屯商圈" in out
    assert "烤鱼、辣子鸡" in out


def test_poi_cards_missing_biz_ext_omits_rating_line():
    poi = _sample_poi()
    del poi["biz_ext"]
    del poi["tel"]
    out = _render_poi_cards({"pois": [poi]})
    assert "评分" not in out
    assert "☎️" not in out
    # 核心字段仍然展示
    assert "肯德基" in out
    assert "三里屯路11号院1号楼" in out


def test_poi_cards_truncate_after_eight_with_note():
    pois = [_sample_poi(name=f"地点{i}") for i in range(10)]
    out = _render_poi_cards({"pois": pois})
    assert out.count("<b>") == 9  # 标题 1 个 + 8 个地点编号
    assert "其余 2 个地点未在卡片中展开" in out


def test_poi_cards_render_first_photo_but_never_coordinates():
    """gaode_mcp 直连后 UI 拿到完整载荷：首张实景图进卡片；
    原始坐标仍然不进卡片（用户可以在地图 App 里搜名字）。"""
    poi = _sample_poi(photos=[{"url": "https://img.example.com/1.jpg"}])
    out = _render_poi_cards({"pois": [poi]})
    assert '<img src="https://img.example.com/1.jpg"' in out
    assert "116.48,39.99" not in out


def test_poi_cards_photo_safety_degrade():
    """非 http(s) URL / 缺失 photos 时安全降级：不产生 img 标签。"""
    poi_bad_scheme = _sample_poi(photos=[{"url": "javascript:alert(1)"}])
    out = _render_poi_cards({"pois": [poi_bad_scheme]})
    assert "<img" not in out

    poi_empty = _sample_poi()  # 默认样例不含 photos 键
    out = _render_poi_cards({"pois": [poi_empty]})
    assert "<img" not in out

    # 多张 photos 只取首张
    poi_many = _sample_poi(photos=[
        {"url": "https://img.example.com/1.jpg"},
        {"url": "https://img.example.com/2.jpg"},
    ])
    out = _render_poi_cards({"pois": [poi_many]})
    assert out.count("<img") == 1
    assert "https://img.example.com/2.jpg" not in out


def test_poi_cards_empty_or_missing_returns_none():
    assert _render_poi_cards({"pois": []}) is None
    assert _render_poi_cards({"status": "0"}) is None


def test_geocode_cards_no_longer_use_per_item_details():
    payload = {
        "return": [
            {"location": "116.48,39.99", "province": "北京市", "city": "北京市", "district": "朝阳区", "level": "兴趣点"},
        ]
    }
    out = _render_map_location_card(payload, "maps_geo")
    assert out is not None
    assert "<details" not in out
    assert "北京市" in out and "朝阳区" in out
    assert "116.48,39.99" in out


def test_map_payload_dispatch_prefers_poi_cards():
    payload = {"pois": [_sample_poi()]}
    assert _render_map_payload(payload, "keyword_search") == _render_poi_cards(payload)
