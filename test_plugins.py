"""插件工具调用测试 - 扫描、格式、执行、LLM 工具调用全链路"""
import os, sys, json
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from core.tool_registry import ToolRegistry
from core.tool_executor import ToolExecutor

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name} {detail}")
        failed += 1

print("╔══════════════════════════════════════════════╗")
print("║  插件工具调用测试                             ║")
print("╚══════════════════════════════════════════════╝")

# ── 1. 工具发现 ──
print("\n[1] 工具发现")
registry = ToolRegistry()
registry.discover()
check("发现工具", registry.tool_count > 0, f"count={registry.tool_count}")
print(f"    共 {registry.tool_count} 个工具")

# 列出前 10 个
tools = registry.get_tools()
sample = tools[:10]
for t in sample:
    name = t["function"]["name"]
    desc = t["function"]["description"][:50]
    print(f"    - {name}: {desc}")
if registry.tool_count > 10:
    print(f"    ... 还有 {registry.tool_count - 10} 个")

# ── 2. 格式校验 ──
print("\n[2] OpenAI 格式校验")
valid_format = True
for t in tools:
    f = t.get("function", {})
    if not f.get("name") or not f.get("name")[0].isalpha():
        valid_format = False
        print(f"    无效名称: {f.get('name')}")
        break
    if not isinstance(f.get("parameters"), dict):
        valid_format = False
        break
check("所有工具名称合法", valid_format)
check("所有工具有 parameters", all("parameters" in t["function"] for t in tools))
check("所有工具有 description", all("description" in t["function"] for t in tools))

# ── 3. 工具查找 ──
print("\n[3] 工具查找")
first_name = tools[0]["function"]["name"]
tool_def = registry.get_tool(first_name)
check("按名称查找", tool_def is not None)
if tool_def:
    check("查找结果含 plugin_id", hasattr(tool_def, "plugin_id"))
    check("查找结果含 source_path", hasattr(tool_def, "source_path"))

# ── 4. Node.js 可用性 ──
print("\n[4] Node.js 执行环境")
import subprocess
try:
    node_ver = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
    check("Node.js 已安装", node_ver.returncode == 0, f"version={node_ver.stdout.strip()}")
    node_available = True
except (FileNotFoundError, subprocess.TimeoutExpired):
    check("Node.js 已安装", False, "未找到 node 命令")
    node_available = False

# ── 5. 工具执行（如果 Node.js 可用）──
if node_available and tool_def and tool_def.source_path:
    print("\n[5] 工具执行")
    executor = ToolExecutor()
    # 用空参数测试执行（不期望成功，只验证执行链路通）
    try:
        result = executor.execute(tool_def, {})
        check("执行链路通畅", isinstance(result, str))
        check("有输出", len(result) > 0)
        print(f"    输出: {result[:100]}")
    except Exception as e:
        check("执行链路通畅", False, str(e))
else:
    print("\n[5] 工具执行 (跳过 - 无 Node.js 或无源文件)")

# ── 6. LLM 工具调用 ──
print("\n[6] LLM 工具调用")
from core.harness_adapter import HanakoPetAdapter
adapter = HanakoPetAdapter(agent_id="yuexinmiao", builtin=True)

# 发送一个可能触发工具调用的消息
test_msg = "帮我搜索一下今天的新闻"
reply, emotion = adapter.chat(test_msg, inject_memory=False, tools=tools[:10])

if isinstance(reply, dict) and reply.get("tool_calls"):
    tc = reply["tool_calls"]
    check("LLM 返回 tool_calls", True)
    print(f"    LLM 调用了 {len(tc)} 个工具")
    for call in tc:
        print(f"    - {call.get('function', {}).get('name', '?')}")
        print(f"      参数: {call.get('function', {}).get('arguments', '')[:80]}")
    check("工具名称在注册表中", any(
        registry.get_tool(c.get("function", {}).get("name", "")) is not None
        for c in tc
    ))
else:
    # LLM 没调工具也正常（可能直接回复了）
    check("LLM 有回复", isinstance(reply, str) and len(reply) > 0, f"reply={reply}")
    print(f"    LLM 直接回复（未调工具）: {reply[:60]}")

# ── 结果 ──
print(f"\n{'='*50}")
print(f"  结果: {passed} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(1 if failed else 0)
