"""路径常量 - 集中管理所有数据文件路径"""
import os
from pathlib import Path

# 项目根目录
PROJECT_DIR = Path(__file__).parent

# 数据目录（outbox/response/pending 都在这里）
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 桥接文件
OUTBOX_FILE = DATA_DIR / "outbox.json"
RESPONSE_FILE = DATA_DIR / "response.json"
NOTIFY_FILE = DATA_DIR / "notifications.json"
PENDING_FLAG = DATA_DIR / ".pending"
