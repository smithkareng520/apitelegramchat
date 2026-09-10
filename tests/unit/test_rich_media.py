from core.rich_media import _enforce_rich_message_max_depth


def _max_details_depth(value: str) -> int:
    import re
    depth = max_depth = 0
    for m in re.finditer(r"<(/?)details\b[^>]*>", value, re.I):
        if m.group(1):
            depth = max(0, depth - 1)
        else:
            depth += 1
            max_depth = max(max_depth, depth)
    return max_depth


def test_depth_guard_flattens_excess_nesting():
    html = "<details>" * 20 + "payload" + "</details>" * 20
    out = _enforce_rich_message_max_depth(html)
    assert _max_details_depth(out) <= 16
    assert "payload" in out


def test_depth_guard_preserves_normal_nested_html():
    html = "<details><summary>x</summary><p><b>ok</b></p></details>"
    assert _enforce_rich_message_max_depth(html) == html
