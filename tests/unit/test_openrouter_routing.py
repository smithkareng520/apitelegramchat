"""OpenRouter provider 路由：全局偏好、模型级 provider_routing、排序变体后缀。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config


@pytest.fixture
def cfg(monkeypatch):
    """固定全局偏好为默认值；用 monkeypatch 改模块变量，不 reload（会污染其他测试）。"""
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_SORT", "price")
    monkeypatch.setattr(config, "OPENROUTER_ALLOW_FALLBACKS", True)
    monkeypatch.setattr(config, "OPENROUTER_REQUIRE_PARAMETERS", False)
    return config


def _model(cfg, model_id, **kwargs):
    return cfg.make_model_config(model_id=model_id, provider="openrouter", **kwargs)


def test_default_sorts_by_price_to_bypass_auto_exacto(cfg):
    assert cfg.get_openrouter_provider_preferences() == {"sort": "price", "allow_fallbacks": True}


def test_global_sort_and_require_parameters_apply(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "OPENROUTER_PROVIDER_SORT", "throughput")
    monkeypatch.setattr(cfg, "OPENROUTER_REQUIRE_PARAMETERS", True)
    assert cfg.get_openrouter_provider_preferences() == {
        "allow_fallbacks": True, "sort": "throughput", "require_parameters": True,
    }


@pytest.mark.parametrize("value, ok", [("Price", True), ("", True), ("fastest", False)])
def test_env_sort_validated_at_import(value, ok):
    src = Path(config.__file__).parent
    env = {**os.environ, "OPENROUTER_PROVIDER_SORT": value, "PYTHONPATH": str(src)}
    proc = subprocess.run(
        [sys.executable, "-c", "import config; print(config.OPENROUTER_PROVIDER_SORT)"],
        env=env, capture_output=True, text=True, cwd=str(src),
    )
    assert (proc.returncode == 0) is ok
    if ok:
        assert proc.stdout.strip() == (value.lower() or "price")


def test_model_routing_overlays_global(cfg):
    model = _model(
        cfg, "google/gemini-3-pro",
        provider_routing={"only": ["google-vertex/europe"], "allow_fallbacks": False},
    )
    prefs = cfg.get_openrouter_provider_preferences(model)
    assert prefs == {"sort": "price", "only": ["google-vertex/europe"], "allow_fallbacks": False}
    assert cfg.get_openrouter_provider_preferences() == {"sort": "price", "allow_fallbacks": True}


def test_model_routing_does_not_mutate_model_config(cfg):
    model = _model(cfg, "a/b", provider_routing={"only": ["x"]})
    cfg.get_openrouter_provider_preferences(model)["only"].append("y")
    assert model.provider_routing == {"only": ["x"]}


@pytest.mark.parametrize("variant", ["nitro", "floor", "exacto"])
def test_sort_variant_drops_global_sort(cfg, monkeypatch, variant):
    monkeypatch.setattr(cfg, "OPENROUTER_PROVIDER_SORT", "price")
    plain = _model(cfg, "a/b")
    suffixed = _model(cfg, f"a/b:{variant}")
    assert cfg.get_openrouter_provider_preferences(plain)["sort"] == "price"
    assert "sort" not in cfg.get_openrouter_provider_preferences(suffixed)


def test_sort_variant_detection(cfg):
    assert cfg.openrouter_sort_variant("a/b") is None
    assert cfg.openrouter_sort_variant("a/b:free") is None
    assert cfg.openrouter_sort_variant("a/b:free:nitro") == "nitro"
    assert cfg.openrouter_sort_variant("a/b:nitro:exacto") == "exacto"
    assert cfg.openrouter_sort_variant("openrouter/a/b:floor") == "floor"


@pytest.mark.parametrize(
    "routing",
    [
        {"onyl": ["x"]},
        {"only": "google-vertex"},
        {"only": []},
        {"allow_fallbacks": "false"},
        {"data_collection": "maybe"},
        {"sort": "fastest"},
        "only",
    ],
)
def test_invalid_provider_routing_rejected(cfg, routing):
    with pytest.raises(ValueError):
        _model(cfg, "a/b", provider_routing=routing)


def test_sort_conflicts_with_variant_suffix(cfg):
    with pytest.raises(ValueError):
        _model(cfg, "a/b:nitro", provider_routing={"sort": "price"})
    assert _model(cfg, "a/b", provider_routing={"sort": "price"}).provider_routing == {"sort": "price"}


def test_provider_routing_only_for_openrouter(cfg):
    with pytest.raises(ValueError):
        cfg.make_model_config(model_id="x", provider="modelscope", provider_routing={"only": ["a"]})


def test_extra_body_uses_model_routing_and_keeps_reasoning(cfg):
    from ai import agentic_loops

    model = _model(
        cfg, "google/gemini-3-pro",
        reasoning_enabled=True,
        provider_routing={"only": ["google-vertex/europe"], "allow_fallbacks": False},
    )
    _, reasoning_extra = cfg.get_reasoning_request_fields(model, "openrouter")
    body = agentic_loops._merged_extra_body(
        "openrouter", reasoning_extra, chat_id=1, model_info=model,
    )
    assert body["provider"] == {
        "sort": "price", "only": ["google-vertex/europe"], "allow_fallbacks": False,
    }
    assert body["reasoning"] == {"enabled": True}
    assert body["session_id"]


@pytest.mark.parametrize("variant", ["nitro", "floor", "exacto"])
def test_route_param_builds_wire_name_and_drops_global_sort(cfg, variant):
    model = _model(cfg, "moonshotai/kimi-k2-0905", route=variant)
    assert cfg.get_wire_model_name("moonshotai/kimi-k2-0905", model) == f"moonshotai/kimi-k2-0905:{variant}"
    assert "sort" not in cfg.get_openrouter_provider_preferences(model)


def test_wire_name_unchanged_without_route(cfg):
    model = _model(cfg, "a/b")
    assert cfg.get_wire_model_name("a/b", model) == "a/b"
    assert cfg.get_wire_model_name("a/b") == "a/b"


def test_route_with_pinned_provider_keeps_other_routing(cfg):
    model = _model(cfg, "a/b", route="floor", provider_routing={"only": ["x"]})
    assert cfg.get_openrouter_provider_preferences(model) == {"allow_fallbacks": True, "only": ["x"]}


@pytest.mark.parametrize(
    "model_id, kwargs",
    [
        ("a/b", {"route": "fastest"}),
        ("a/b:nitro", {"route": "floor"}),
        ("a/b", {"route": "floor", "provider_routing": {"sort": "price"}}),
    ],
)
def test_invalid_route_rejected(cfg, model_id, kwargs):
    with pytest.raises(ValueError):
        _model(cfg, model_id, **kwargs)


def test_route_only_for_openrouter(cfg):
    with pytest.raises(ValueError):
        cfg.make_model_config(model_id="x", provider="modelscope", route="nitro")


def test_request_model_name_carries_route_suffix(cfg):
    """主循环三处请求构造都经 get_wire_model_name，不再直接发配置里的模型名。"""
    import inspect

    from ai import agentic_loops

    src = inspect.getsource(agentic_loops)
    assert '"model": current_model,' not in src
    assert src.count('"model": get_wire_model_name(current_model, model_info),') == 3
