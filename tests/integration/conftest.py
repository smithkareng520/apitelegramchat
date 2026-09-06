# =====================================================================
# tests/integration/conftest.py — 集成测试环境准备
# =====================================================================
# 必须在导入任何项目模块（尤其是 config.py / app.py）之前设置环境变量：
#   - APITELEGRAMCHAT_DATA_DIR 指向独立临时目录，避免污染真实数据目录；
#   - WEBHOOK_TOKEN / TELEGRAM_BOT_TOKEN 提供测试凭据；
#   - INGEST_MODE=polling 与生产部署一致（webhook 入口保持鉴权可探活）。
# =====================================================================
import os
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="apitelegramchat_it_"))
os.environ.setdefault("APITELEGRAMCHAT_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TEST_TOKEN_FOR_INTEGRATION_TESTS")
os.environ.setdefault("WEBHOOK_TOKEN", "it-webhook-secret-token")
os.environ.setdefault("INGEST_MODE", "polling")
os.environ.setdefault("PORT", "5999")
