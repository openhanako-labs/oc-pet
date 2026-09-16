"""DISC-1（2026-09-14）测试：Hana 全体系发现器。

覆盖：
  - 五套体系各自能发现
  - 双通道（API + 静态回退）
  - server 不可达时的降级
  - 只读清单不读数据
  - TTL 缓存
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import hana_catalog as hc  # noqa: E402


# ── 常量与路径 ──


def test_paths_under_hanako_home():
    for p in (hc.SERVER_INFO, hc.PLUGINS_DIR, hc.APPS_DIR,
              hc.SKILLS_DIR, hc.AGENTS_DIR, hc.MCP_CONFIG):
        assert str(p).startswith(str(Path.home() / ".hanako"))


def test_default_ttl_reasonable():
    """TTL 不能太长（状态会变）也不能太短（扫描有成本）。"""
    assert 10 <= hc.DEFAULT_TTL <= 300


# ── server-info ──


def test_read_server_info_returns_port_token():
    info = hc._read_server_info()
    if info is None:
        pytest.skip("Hana server 未运行")
    assert isinstance(info["port"], int)
    assert info["port"] > 0
    assert info["token"]


def test_read_server_info_never_caches(monkeypatch, tmp_path):
    """★ token 每次重启会变 → 必须每次重读，不许缓存。"""
    calls = []

    fake = tmp_path / "server-info.json"
    fake.write_text(json.dumps({"port": 1, "token": "a"}), encoding="utf-8")
    monkeypatch.setattr(hc, "SERVER_INFO", fake)

    r1 = hc._read_server_info()
    calls.append(r1)
    # 改内容
    fake.write_text(json.dumps({"port": 2, "token": "b"}), encoding="utf-8")
    r2 = hc._read_server_info()
    calls.append(r2)

    assert r1["token"] == "a"
    assert r2["token"] == "b", "读到旧 token = 缓存了，重启后会失效"
    assert r2["port"] == 2


def test_read_server_info_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "SERVER_INFO", tmp_path / "nope.json")
    assert hc._read_server_info() is None


def test_read_server_info_malformed(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(hc, "SERVER_INFO", f)
    assert hc._read_server_info() is None


def test_read_server_info_missing_token(monkeypatch, tmp_path):
    f = tmp_path / "partial.json"
    f.write_text(json.dumps({"port": 1234}), encoding="utf-8")
    monkeypatch.setattr(hc, "SERVER_INFO", f)
    assert hc._read_server_info() is None


# ── 静态发现（不依赖 server）──


def test_discover_plugins_static():
    """扫 `~/.hanako/plugins`。

    ★ 2026-09-16 CI 修复：这是**用户本机目录**，CI 干净环境不存在，
    原先断言 count>0 直接失败。本地有插件时照常验证。
    """
    if not hc.PLUGINS_DIR.is_dir():
        pytest.skip(f"无用户插件目录: {hc.PLUGINS_DIR}")
    r = hc._discover_plugins()
    assert r["count"] > 0
    assert all("id" in it for it in r["items"])
    assert all("has_tools" in it for it in r["items"])


def test_discover_apps_only_v2():
    """只认 manifestVersion==2 的应用。"""
    r = hc._discover_apps()
    for it in r["items"]:
        assert it.get("capabilities") is not None


def test_discover_mcp_reads_config():
    r = hc._discover_mcp()
    if hc.MCP_CONFIG.is_file():
        assert r["count"] >= 0
        for it in r["items"]:
            assert "enabled" in it
            assert "tool_count" in it


def test_discover_skills_handles_missing_skill_md():
    """★ 实测例外：92 个目录中 expert/ 没有 SKILL.md，必须容错跳过。

    ★ 2026-09-16 CI 修复：扫 `~/.hanako/skills`（用户本机目录），
    CI 干净环境不存在 → skip。
    """
    if not hc.SKILLS_DIR.is_dir():
        pytest.skip(f"无用户技能目录: {hc.SKILLS_DIR}")
    r = hc._discover_skills()
    assert r["count"] > 0
    for it in r["items"]:
        assert it["name"]


def test_discover_agents():
    r = hc._discover_agents()
    assert r["count"] >= 0


# ── 工具文件解析 ──


def test_scan_tool_files_parses_exports(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "foo.js").write_text(
        "export const name = 'foo_tool';\n"
        "export const description = '一个测试工具';\n"
        "export async function execute() {}\n",
        encoding="utf-8",
    )
    out = hc._scan_tool_files(d)
    assert len(out) == 1
    assert out[0]["name"] == "foo_tool"
    assert out[0]["description"] == "一个测试工具"


def test_scan_tool_files_handles_single_quotes(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "bar.js").write_text(
        "export const name = \"bar_tool\";\n"
        "export const description = \"单引号也要认\";\n",
        encoding="utf-8",
    )
    out = hc._scan_tool_files(d)
    assert out[0]["name"] == "bar_tool"


def test_scan_tool_files_missing_dir(tmp_path):
    assert hc._scan_tool_files(tmp_path / "nope") == []


def test_scan_tool_files_ignores_non_js(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "readme.md").write_text("not a tool", encoding="utf-8")
    assert hc._scan_tool_files(d) == []


# ── frontmatter ──


def test_parse_frontmatter(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\n"
        "name: my-skill\n"
        "description: \"这是一个技能\"\n"
        "trigger_keywords:\n"
        "  - 关键词一\n"
        "  - 关键词二\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    info = hc._parse_frontmatter(f)
    assert info["name"] == "my-skill"
    assert info["description"] == "这是一个技能"
    assert "关键词一" in info["trigger_keywords"]


def test_parse_frontmatter_no_frontmatter(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("# 没有 frontmatter\n", encoding="utf-8")
    assert hc._parse_frontmatter(f) == {}


def test_parse_frontmatter_missing_file(tmp_path):
    assert hc._parse_frontmatter(tmp_path / "nope.md") == {}


# ── 目录聚合与缓存 ──


def test_catalog_has_all_five_systems():
    c = hc.get_catalog(force=True)
    for k in ("plugins", "apps", "mcp", "skills", "agents"):
        assert k in c
        assert "count" in c[k]
    assert set(c["totals"]) == {"plugins", "apps", "mcp_connectors", "skills", "agents"}


def test_catalog_cache_hit():
    """TTL 内第二次调用必须命中缓存（不重复扫描）。"""
    cat = hc.HanaCatalog(ttl=60)
    a = cat.get()
    b = cat.get()
    assert a is b


def test_catalog_force_bypasses_cache():
    cat = hc.HanaCatalog(ttl=60)
    a = cat.get()
    b = cat.get(force=True)
    assert a is not b


def test_catalog_ttl_zero_always_refreshes():
    cat = hc.HanaCatalog(ttl=0)
    a = cat.get()
    b = cat.get()
    assert a is not b


def test_catalog_degrades_without_server(monkeypatch):
    """★ server 不可达时仍须返回静态可读的部分。"""
    monkeypatch.setattr(hc, "_read_server_info", lambda: None)
    cat = hc.HanaCatalog(ttl=0)
    c = cat.get()
    assert c["hana_server_reachable"] is False
    # 静态扫描仍应有结果
    assert c["totals"]["plugins"] >= 0
    assert c["totals"]["skills"] >= 0


def test_catalog_survives_section_failure(monkeypatch):
    """单个体系扫描失败不能拖垮整体。"""
    def boom():
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(hc, "_discover_plugins", boom)
    cat = hc.HanaCatalog(ttl=0)
    try:
        c = cat.get()
    except Exception as e:
        pytest.fail(f"单体系失败导致整体崩溃: {e}")


def test_summary_is_lightweight():
    """summary 只给统计，不给明细（供 MCP/日志用）。"""
    cat = hc.HanaCatalog(ttl=60)
    s = cat.summary()
    assert "totals" in s
    assert "hana_server_reachable" in s
    assert "items" not in json.dumps(s)


# ── 范围边界：只读清单，不读数据 ──


def test_no_user_data_read():
    """★ 发现器不得读取用户数据内容（待办正文/笔记内容等）。

    只允许读清单类文件（manifest/config/SKILL.md frontmatter）。
    """
    import inspect
    src = inspect.getsource(hc)
    # 这些是明确的用户数据文件，不该出现在发现器里
    forbidden = [
        "todos.json", "playlist.json", "stickers",
        "notes", "messages", "session.jsonl",
    ]
    for f in forbidden:
        assert f not in src, f"发现器不应读取用户数据: {f}"


def test_only_localhost():
    """★ 安全：只连 127.0.0.1，不用 server-info 广告的内网 IP。"""
    import inspect
    src = inspect.getsource(hc._api_get)
    assert "127.0.0.1" in src
    assert "advertisedHost" not in src
    assert "virtualLanHosts" not in src
