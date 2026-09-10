"""P1 集成收尾 — pet.py 四线接线离屏单测。

把 P1 四线（A 语义检索 / B 事实库+反思 / C 反重复+屏幕感知 / D 角色卡+HUD）
在 ``PetWindow`` 里的接线逻辑抽出验证：

- C 线：proactive.set_anti_repeat / set_screen_scene_provider；
        screen.set_llm_enrich / set_enrich_provider（adapter 包装 source="screen_enrich"）
- B 线：FactStore 创建 + ``_record_conversation_facts`` 对话事实写入；
        ReflectionEngine 创建 + ``_maybe_reflect`` 定时触发
- A 线：``_init_p1_embedding_check`` 确认默认 provider 注入不崩
- D 线：角色卡入口存在（PetWindow._init_neko_panels 注册 manage 菜单动作）

全部用最小 fake harness + monkeypatch，不构造 PetWindow、不触碰真实记忆目录。
无显示器环境用 offscreen QPA 平台运行：
    python -m pytest tests/test_p1_integration_wiring.py -v
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import pet  # noqa: E402  (验证 PetWindow 可导入 + 新方法存在)


# ── 最小 fake harness ─────────────────────────────────────

class _FakeEngine:
    def __init__(self, adapter=None):
        self._adapter = adapter


class _FakeAdapter:
    """模拟 HanakoPetAdapter：记录 chat_direct 调用参数。"""
    def __init__(self):
        self.calls = []

    def chat_direct(self, message, inject_memory=True, extra_context="",
                    tools=None, source="user"):
        self.calls.append({
            "message": message,
            "inject_memory": inject_memory,
            "source": source,
        })
        return '{"scene": "coding", "confidence": 0.8}', "neutral"


class _FakeProactive:
    def __init__(self):
        self.calls = []

    def set_anti_repeat(self, corpus=None, agent_name=""):
        self.calls.append(("set_anti_repeat", corpus, agent_name))

    def set_screen_scene_provider(self, provider):
        self.calls.append(("set_screen_scene_provider", provider))

    def set_scene_memory(self, scene_memory):
        self.calls.append(("set_scene_memory", scene_memory))

    def load_memory_config(self, cfg):
        self.calls.append(("load_memory_config", cfg))


class _FakeScreen:
    def __init__(self):
        self.llm_enrich = None
        self.provider = None

    def set_llm_enrich(self, enabled):
        self.llm_enrich = bool(enabled)

    def set_enrich_provider(self, provider):
        self.provider = provider

    def get_scene_snapshot(self):
        return {"scene": "coding", "intent": "work", "confidence": 0.8}


class _FakePerception:
    def __init__(self):
        self.screen = _FakeScreen()

    @property
    def emotion(self):
        return None


class _FakePet:
    """最小 PetWindow 替身：只提供 P1 接线方法用到的属性。"""
    def __init__(self):
        self.config = {
            "anti_repeat": {"enabled": True},
            "screen": {"llm_enrich": True},
            "memory": {
                "facts": {"enabled": True},
                "reflection": {"enabled": True},
                "embedding": {"enabled": False},
            },
        }
        self._current_char = "t1"
        self._agent_id = "t1"
        self._proactive = _FakeProactive()
        self._perception = _FakePerception()
        self._engine = _FakeEngine(adapter=_FakeAdapter())
        self._event_stream = None
        self._fact_store = None
        self._reflection_engine = None

    def _on_fact_store_changed(self, result):
        pass

    def _on_reflection_changed(self, result):
        pass


# ── C 线：反重复 + 屏幕感知 ──────────────────────────────

def test_p1_anti_repeat_wiring(monkeypatch):
    from core import anti_repeat as ar_mod
    sentinel = object()
    monkeypatch.setattr(ar_mod, "get_anti_repeat_corpus", lambda *a, **k: sentinel)

    fake = _FakePet()
    pet.PetWindow._init_p1_anti_repeat(fake)
    calls = fake._proactive.calls
    assert any(c[0] == "set_anti_repeat" for c in calls)
    match = next(c for c in calls if c[0] == "set_anti_repeat")
    assert match[1] is sentinel          # corpus
    assert match[2] == "t1"              # agent_name


def test_p1_anti_repeat_disabled(monkeypatch):
    from core import anti_repeat as ar_mod
    monkeypatch.setattr(ar_mod, "get_anti_repeat_corpus", lambda *a, **k: object())
    fake = _FakePet()
    fake.config["anti_repeat"] = {"enabled": False}
    pet.PetWindow._init_p1_anti_repeat(fake)
    assert not any(c[0] == "set_anti_repeat" for c in fake._proactive.calls)


def test_p1_screen_enrich_wiring():
    fake = _FakePet()
    pet.PetWindow._init_p1_screen_enrich(fake)
    # proactive 注入场景 provider
    assert any(c[0] == "set_screen_scene_provider" for c in fake._proactive.calls)
    # screen 开 LLM 增强 + 注入 adapter 包装 provider
    assert fake._perception.screen.llm_enrich is True
    assert fake._perception.screen.provider is not None
    # provider 走 adapter.chat_direct(source="screen_enrich")
    result = fake._perception.screen.provider("prompt-xyz")
    assert result is not None
    adapter = fake._engine._adapter
    assert adapter.calls and adapter.calls[-1]["source"] == "screen_enrich"
    assert adapter.calls[-1]["inject_memory"] is False


def test_p1_screen_enrich_no_adapter():
    fake = _FakePet()
    fake._engine = _FakeEngine(adapter=None)
    pet.PetWindow._init_p1_screen_enrich(fake)
    assert fake._perception.screen.provider is None   # 无适配器 → 纯规则
    assert fake._perception.screen.llm_enrich is True


def test_p1_screen_enrich_disabled_by_config():
    fake = _FakePet()
    fake.config["screen"] = {"llm_enrich": False}
    pet.PetWindow._init_p1_screen_enrich(fake)
    assert fake._perception.screen.llm_enrich is False
    assert fake._perception.screen.provider is None


# ── B 线：事实库 + 反思引擎 ──────────────────────────────

def test_p1_fact_store_wiring(monkeypatch):
    from core import memory_facts as mf_mod

    class FakeFactStore:
        instances = []

        def __init__(self, **kw):
            self.kw = kw
            self.recorded = []
            self.cb = None
            FakeFactStore.instances.append(self)

        def set_changed_callback(self, cb):
            self.cb = cb

        def record_text(self, text, extra_context="", evidence=None):
            self.recorded.append((text, extra_context, evidence))

    monkeypatch.setattr(mf_mod, "FactStore", FakeFactStore)
    fake = _FakePet()
    pet.PetWindow._init_p1_fact_store(fake)
    store = fake._fact_store
    assert store is FakeFactStore.instances[-1]
    assert store.kw["agent_id"] == "t1"
    assert store.kw["use_qt_bridge"] is True
    # 对话事实写入点
    pet.PetWindow._record_conversation_facts(fake, "我喜欢深夜写代码")
    assert store.recorded
    text, extra, evidence = store.recorded[0]
    assert text == "我喜欢深夜写代码"
    assert extra == "对话"
    assert evidence and evidence[0]["source"] == "conversation"
    # 空文本 / 无 store 不崩
    pet.PetWindow._record_conversation_facts(fake, "   ")
    fake._fact_store = None
    pet.PetWindow._record_conversation_facts(fake, "x")


def test_p1_fact_store_disabled():
    fake = _FakePet()
    fake.config["memory"]["facts"] = {"enabled": False}
    pet.PetWindow._init_p1_fact_store(fake)
    assert fake._fact_store is None


def test_p1_reflection_wiring(monkeypatch):
    from core import memory_reflection as mr_mod

    class FakeReflectionEngine:
        instances = []

        def __init__(self, **kw):
            self.kw = kw
            self.cb = None
            FakeReflectionEngine.instances.append(self)

        def set_changed_callback(self, cb):
            self.cb = cb

        def schedule_reflect(self, force=False):
            self.kw["scheduled"] = True

    monkeypatch.setattr(mr_mod, "ReflectionEngine", FakeReflectionEngine)
    fake = _FakePet()
    fake._event_stream = object()   # 传入 event_source
    pet.PetWindow._init_p1_reflection(fake)
    engine = fake._reflection_engine
    assert engine is FakeReflectionEngine.instances[-1]
    assert engine.kw["agent_id"] == "t1"
    assert engine.kw["event_source"] is fake._event_stream
    assert engine.kw["use_qt_bridge"] is True
    # 定时触发：maybe_reflect → schedule_reflect
    pet.PetWindow._maybe_reflect(fake)
    assert engine.kw.get("scheduled") is True
    # 无引擎不崩
    fake._reflection_engine = None
    pet.PetWindow._maybe_reflect(fake)


def test_p1_reflection_disabled():
    fake = _FakePet()
    fake.config["memory"]["reflection"] = {"enabled": False}
    pet.PetWindow._init_p1_reflection(fake)
    assert fake._reflection_engine is None


# ── A 线：向量嵌入确认 ───────────────────────────────────

def test_p1_embedding_check_no_crash(monkeypatch):
    from core import memory_hybrid as mh_mod
    monkeypatch.setattr(mh_mod, "_default_embedding_provider", lambda: None)
    fake = _FakePet()
    pet.PetWindow._init_p1_embedding_check(fake)   # 不抛即通过


def test_p1_embedding_check_provider_available(monkeypatch):
    from core import memory_hybrid as mh_mod
    monkeypatch.setattr(mh_mod, "_default_embedding_provider",
                        lambda: object())
    fake = _FakePet()
    fake.config["memory"]["embedding"] = {"enabled": True}
    pet.PetWindow._init_p1_embedding_check(fake)   # 不抛即通过


# ── D 线：角色卡入口确认 ─────────────────────────────────

def test_character_card_entry_exists():
    """D 线：PetWindow 已有角色卡菜单接线方法（P1-7 已交付）。

    ``_character_card`` 是实例属性（_init_neko_panels 里创建），类上只保证
    方法存在 + ``_init_neko_panels`` 源码注册了「🪪 角色卡」菜单动作。
    """
    import inspect
    assert hasattr(pet.PetWindow, "_toggle_character_card")
    src = inspect.getsource(pet.PetWindow._init_neko_panels)
    assert "🪪 角色卡" in src
    assert "CharacterCard" in src


def test_p1_integration_entry_exists():
    """集成总入口 + 各子接线方法存在。"""
    for name in ("_init_neko_p1", "_init_p1_anti_repeat",
                 "_init_p1_screen_enrich", "_init_p1_fact_store",
                 "_init_p1_reflection", "_init_p1_embedding_check",
                 "_record_conversation_facts", "_maybe_reflect"):
        assert hasattr(pet.PetWindow, name), f"PetWindow 缺 {name}"
