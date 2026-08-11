from pathlib import Path


def test_bash_handles_outer_cancellation_before_timeout():
    source = Path("src/apitelegramchat/tool_executors.py").read_text(encoding="utf-8")
    marker = "except asyncio.CancelledError:"
    assert marker in source
    assert 'await asyncio.shield(self.close())' in source
    assert 'Bash execution cancelled; killing session' in source
