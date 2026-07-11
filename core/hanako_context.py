"""Hanako 上下文读取器 — 从 Hanako 本体文件读取角色设定、模型配置和记忆

替代方案：不再使用 skills/public/<角色>/SKILL.md 和独立的 config.json API 配置，
而是直接读取 ~/.hanako/agents/<角色>/ 下的同一套文件。

用法：
    ctx = HanakoContext("ophelia")
    identity = ctx.read_identity()       # identity.md → 角色身份
    ishiki = ctx.read_ishiki()           # ishiki.md → 意识/规则
    system_prompt = ctx.build_prompt()   # 组合成完整 system prompt
    model_cfg = ctx.read_model_config()  # 模型配置
    memory = ctx.read_memory()           # 最近记忆
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


def _read_file(path: Path) -> str:
    """安全读取文件内容"""
    try:
        if path.exists():
            return path.read_text("utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
    return ""


class HanakoContext:
    """读取 Hanako Agent 的配置文件，提供统一的上下文接口。

    支持两种角色来源：
    - Hanako agent: ~/.hanako/agents/<agent_id>/
    - 内置角色: <project>/characters/<agent_id>/ (builtin)
    """

    def __init__(self, agent_id: str = "ophelia", builtin: bool = False):
        self.agent_id = agent_id
        self._builtin = builtin
        if builtin:
            # 内置角色从项目目录读取
            self._agent_dir = Path(__file__).parent.parent / "characters" / agent_id
        else:
            self._agent_dir = HANAKO_HOME / "agents" / agent_id
        self._provider_catalog = self._load_provider_catalog()

    # ── 角色设定 ──

    def read_identity(self) -> str:
        """读取 identity.md — 角色最核心的身份定义"""
        return _read_file(self._agent_dir / "identity.md")

    def read_ishiki(self) -> str:
        """读取 ishiki.md — 底层意识/行为规则/对话约束"""
        return _read_file(self._agent_dir / "ishiki.md")

    def read_public_ishiki(self) -> str:
        """读取 public-ishiki.md — 对外可见的意识"""
        return _read_file(self._agent_dir / "public-ishiki.md")

    def read_description(self) -> str:
        """读取 description.md — 角色简要描述"""
        raw = _read_file(self._agent_dir / "description.md")
        return self._strip_html_comment(raw)

    def read_pinned(self) -> str:
        """读取 pinned.md — 置顶记忆/长期规则"""
        raw = _read_file(self._agent_dir / "pinned.md")
        return self._strip_html_comment(raw)

    def read_pinned_memory(self) -> str:
        """读取 pinned-memory.json — 结构化置顶记忆"""
        path = self._agent_dir / "pinned-memory.json"
        try:
            if path.exists():
                data = json.loads(path.read_text("utf-8"))
                if isinstance(data, list):
                    return "\n".join(
                        f"- {item.get('content', '')}"
                        for item in data
                    )
        except Exception as e:
            logger.warning("Failed to read pinned-memory: %s", e)
        return ""

    def build_prompt(self) -> str:
        """组合所有角色设定文件为完整的 system prompt

        顺序：identity → description → public-ishiki → ishiki → pinned
        """
        parts = []
        identity = self.read_identity()
        if identity:
            parts.append(identity)

        desc = self.read_description()
        if desc:
            parts.append(f"\n{desc}")

        pub_ishiki = self.read_public_ishiki()
        if pub_ishiki:
            parts.append(f"\n{pub_ishiki}")

        ishiki = self.read_ishiki()
        if ishiki:
            parts.append(f"\n{ishiki}")

        pinned = self.read_pinned()
        if pinned:
            parts.append(f"\n【置顶规则】\n{pinned}")

        pinned_mem = self.read_pinned_memory()
        if pinned_mem:
            parts.append(f"\n【置顶记忆】\n{pinned_mem}")

        return "\n\n".join(parts)

    # ── 模型配置 ──

    def _load_provider_catalog(self) -> dict:
        """加载 provider-catalog.json"""
        path = HANAKO_HOME / "provider-catalog.json"
        try:
            if path.exists():
                return json.loads(path.read_text("utf-8"))
        except Exception as e:
            logger.warning("Failed to load provider catalog: %s", e)
        return {}

    def read_agent_config(self) -> dict:
        """读取 agent 的 config.yaml（使用 PyYAML）"""
        path = self._agent_dir / "config.yaml"
        if not path.exists():
            return {}
        try:
            import yaml
            with path.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Failed to parse config.yaml: %s", e)
            return {}

    def read_model_config(self) -> dict:
        """读取 Hanako 的模型配置

        Returns:
            {"provider": provider_id, "model": model_id,
             "base_url": "...", "api_key": "...", "api_type": "openai-completions",
             "max_context": int}
            如果找不到完整配置则返回空 dict
        """
        agent_cfg = self.read_agent_config()

        # 从 models.chat 读取
        models = agent_cfg.get("models", {})
        if not isinstance(models, dict):
            return {}

        chat_model = models.get("chat", {})
        if not isinstance(chat_model, dict):
            return {}

        provider_id = chat_model.get("provider", "") or ""
        model_id = chat_model.get("id", "") or ""

        if not provider_id or not model_id:
            logger.warning("Model config incomplete: provider=%s model=%s", provider_id, model_id)
            return {}

        # 从 provider catalog 查找 provider 配置
        providers = self._provider_catalog.get("providers", {})
        provider_cfg = providers.get(provider_id, {})

        if not provider_cfg:
            logger.warning("Provider '%s' not found in catalog", provider_id)
            return {
                "provider": provider_id,
                "model": model_id,
            }

        # 从 catalog 的 models 列表中查找匹配模型的 context 字段
        max_context = 0
        catalog_models = provider_cfg.get("models", [])
        for m in catalog_models:
            if isinstance(m, dict) and m.get("id") == model_id:
                max_context = m.get("context", 0)
                break
            elif isinstance(m, str) and m == model_id:
                break

        result = {
            "provider": provider_id,
            "model": model_id,
            "base_url": provider_cfg.get("base_url", ""),
            "api_key": provider_cfg.get("api_key", ""),
            "api_type": provider_cfg.get("api", "openai-completions"),
        }
        if max_context:
            result["max_context"] = max_context
        return result

    # ── 记忆 ──

    def read_memory(self) -> str:
        """读取 memory.md — 上下文记忆"""
        return _read_file(self._agent_dir / "memory" / "memory.md")

    def read_today(self) -> str:
        """读取 today.md — 今日状态"""
        return _read_file(self._agent_dir / "memory" / "today.md")

    def read_facts(self) -> str:
        """读取 facts.md — 事实知识"""
        raw = _read_file(self._agent_dir / "memory" / "facts.md")
        return self._strip_html_comment(raw)

    def read_longterm(self) -> str:
        """读取 longterm.md — 长期记忆"""
        return _read_file(self._agent_dir / "memory" / "longterm.md")

    def build_memory_context(self, max_chars: int = 1000) -> str:
        """组合记忆文件为上下文摘要"""
        parts = []
        total = 0

        today = self.read_today()
        if today:
            parts.append(f"【今日】\n{today[:300]}")
            total += len(parts[-1])

        if total < max_chars:
            facts = self.read_facts()
            if facts:
                remaining = max_chars - total
                parts.append(f"【事实】\n{facts[:remaining]}")
                total += len(parts[-1])

        if total < max_chars:
            memory = self.read_memory()
            if memory:
                remaining = max_chars - total
                parts.append(f"【记忆】\n{memory[:remaining]}")

        return "\n\n".join(parts)

    # ── 工具方法 ──

    @staticmethod
    def _strip_html_comment(text: str) -> str:
        """移除 HTML 注释 <!-- ... -->"""
        return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()

    def validate(self) -> list[str]:
        """验证所有配置文件的完整性，返回缺失的文件列表"""
        required = [
            "identity.md",
            "ishiki.md",
            "description.md",
        ]
        missing = []
        for f in required:
            if not (self._agent_dir / f).exists():
                missing.append(f)

        # 检查模型配置
        model_cfg = self.read_model_config()
        if not model_cfg.get("base_url"):
            missing.append("model config (base_url not found)")
        if not model_cfg.get("api_key"):
            missing.append("model config (api_key not found)")

        return missing