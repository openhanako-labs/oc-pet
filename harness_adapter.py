"""Harness adapter for OC Desktop Pet — 轻量版。

从 skills/public/<角色名>/SKILL.md 读取角色设定，
通过 Chat Completions API 调用 LLM，返回角色回复。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


# ── 工具函数 ────────────────────────────────────────────

def _list_skill_dirs(skills_root: Path) -> list[Path]:
    """列出 skills/public/ 下所有含 SKILL.md 的子目录。"""
    public_dir = skills_root / "public"
    if not public_dir.is_dir():
        return []
    return sorted([
        d for d in public_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    ])


def _read_skill_body(skill_dir: Path) -> str:
    """读取 skill_dir/SKILL.md，去掉 YAML frontmatter，返回正文。"""
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    body = re.sub(
        r"^---\s*\n.*?\n---\s*\n",
        "",
        content,
        count=1,
        flags=re.DOTALL,
    ).strip()
    return body


# ── 适配器 ──────────────────────────────────────────────

class HarnessPetAdapter:
    """桌宠适配器：Skills 读角色设定 → API 对话 → 返回回复。

    不再依赖 deer-flow Agent 引擎，只保留 Skills 读取和简单 API 调用。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

        # 扫描 skills/public/ 缓存角色 prompt
        skills_root = Path.cwd().resolve() / "skills"
        self._prompts: dict[str, str] = {}
        for d in _list_skill_dirs(skills_root):
            char_id = d.name  # 目录名 = 角色 ID
            body = _read_skill_body(d)
            if body:
                self._prompts[char_id] = body

        # 从旧 config 回退（如果有）
        from config import CHARACTER_INFO
        for cid, info in CHARACTER_INFO.items():
            if cid not in self._prompts or not self._prompts[cid]:
                fallback = info.get("prompt", "")
                if fallback:
                    self._prompts[cid] = fallback

        logger.info("HarnessPetAdapter ready | model=%s | characters=%s",
                     model, list(self._prompts.keys()))

    # ── 公开接口 ─────────────────────────────────────────

    def chat(self, character_id: str, message: str, history: list | None = None) -> str:
        """发送消息，返回角色回复。"""
        prompt = self._prompts.get(character_id)
        if not prompt:
            return "...（我不知道该说什么）"

        messages = [{"role": "system", "content": prompt}]

        # 追加历史（最近 10 轮）
        if history:
            safe = []
            for turn in history:
                role = turn.get("role", "")
                if role in ("user", "assistant") and isinstance(turn.get("content"), str):
                    safe.append({"role": role, "content": turn["content"]})
            messages.extend(safe[-10:])

        messages.append({"role": "user", "content": message.strip()})

        try:
            resp = self._session.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 300,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            return text or "…"
        except requests.exceptions.Timeout:
            return "...（网络有点慢，你再说一遍？）"
        except requests.exceptions.ConnectionError:
            return "...（连不上——检查一下网络和 API 配置吧）"
        except Exception as e:
            logger.warning("Chat failed: %s", e)
            return f"...（出了点岔子：{e}）"
