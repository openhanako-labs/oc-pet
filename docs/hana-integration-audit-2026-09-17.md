# oc-pet「自造 vs 复用 Hana」排查报告

日期：2026-09-17
范围：oc-pet 全部「读取 Hana 侧配置/能力/数据」的代码
方法：grep 全部 `Path.home()/.hanako` 读取点（18 处）+ 对照 Hana bundle 的真实字段定义

---

## 一、结论速览

**根因是一个共性缺陷，不是三个孤立的 bug。**

oc-pet 读 Hana 数据时，**把「数据目录」写死成 `Path.home() / ".hanako"`**（18 处），
而 Hana 官方的解析规则是 **`HANA_HOME` 环境变量优先，默认才是 `~/.hanako`**：

```js
// Hana bundle 里的官方逻辑
function r({ envValue: d, packaged: h, homeDir: p = t.homedir() } = {}) {
  if (d && typeof d == "string")
    return i(d, p);                          // ← HANA_HOME 优先
  if (h)
    return e.resolve(e.join(p, ".hanako"));  // ← 没设才用默认
  throw new Error("HANA_HOME is not set. ...");
}
```

**实测本机 `HANA_HOME` 已设置**（`C:\Users\Administrator\.hanako`，进程级注入，
不在注册表）。当前值恰好等于默认值，所以**今天没暴露**——
但这是巧合，不是正确性。

### 后果分级

| 级别 | 问题 | 触发条件 | 后果 |
|---|---|---|---|
| **P0** | 18 处路径全部写死 `~/.hanako` | 用户设 `HANA_HOME` 指向别处 | oc-pet 读**旧目录/空目录**，全部配置失效 |
| **P1** | ASR 只读 `.env`，不读 Hana 的 `speechRecognition` | 用户在 Hana 设置页配了语音识别 | 改了没反应 |
| **P1** | TTS 只读 `.env`，不读 Hana 侧 | 同上 | 改了没反应 |
| **P2** | LLM 直连配置 `.env` 优先于 Hana | 用户在 Hana 改聊天模型 | oc-pet 直连时仍用旧模型 |

---

## 二、逐项对照

### 1. 数据目录解析 —— 【重复造轮子，P0】

| | |
|---|---|
| **位置** | 18 处，见下表 |
| **oc-pet 现在读** | `Path.home() / ".hanako"`（硬编码） |
| **Hana 权威来源** | `HANA_HOME` 环境变量，回退 `~/.hanako` |
| **判断** | **重复造轮子** |

18 处清单：

```
env_config.py:57     provider-catalog.json
env_config.py:140    server-info.json
env_config.py:189    agents/<id>/config.yaml
env_config.py:238    user/preferences.json      ← 今天新加
env_config.py:314    user/preferences.json      ← 今天新加
pet_manager.py:28    HANAKO_HOME
pet.py:3014          agents/
capability_registry.py:309  plugin-data/hanako-audio-player/playlist.json
conversation_engine.py:998  plugins/
hana_catalog.py:48   HANAKO_HOME
hanako_context.py:23 HANAKO_HOME
harness_adapter.py:151      provider-catalog.json
harness_adapter.py:271      user/preferences.json   ← 今天新加
harness_adapter.py:1078     agents/<id>/pet
startup_check.py:90  hanako_home
tool_executor.py:27  plugins/
tool_executor.py:28  HANAKO_HOME
tool_registry.py:23  plugins/
tool_registry.py:24  apps/
schedule.py:26       HANAKO_HOME
```

**注意**：今天新加的三处（preferences 读取）也沿用了这个坏模式——
我照抄了周围代码的写法，没质疑它。**这正是「自造」的传染性。**

**修法**：一处 `hanako_home()` helper，18 处全改：

```python
def hanako_home() -> Path:
    """Hana 数据目录：HANA_HOME 环境变量优先，回退 ~/.hanako。

    与 Hana 官方解析逻辑一致（bundle 里的 r() 函数）。
    写死 ~/.hanako 会在用户自定义 HANA_HOME 时读到错误目录。
    """
    env = os.environ.get("HANA_HOME", "").strip()
    return Path(env) if env else Path.home() / ".hanako"
```

---

### 2. ASR / 语音识别 —— 【重复造轮子，P1】

| | |
|---|---|
| **位置** | `env_config.py:112 get_asr_api_config()` |
| **oc-pet 现在读** | `.env` 的 `ASR_BASE_URL` / `ASR_API_KEY` / `ASR_MODEL` |
| **Hana 权威来源** | `preferences.json` → `speechRecognition.defaultModel`（Hana 设置页可改） |
| **判断** | **重复造轮子** |

实测 Hana 侧已有配置：

```json
"speechRecognition": {"enabled": true,
                      "defaultModel": {"provider": "openai", "id": "whisper-1"}}
```

oc-pet 完全不读。用户在 Hana 设置页换语音识别模型 → **改了没反应**。

---

### 3. TTS —— 【重复造轮子（部分），P1】

| | |
|---|---|
| **位置** | `env_config.py:85 get_tts_api_config()` |
| **oc-pet 现在读** | `.env` 的 `TTS_*` |
| **Hana 权威来源** | Hana bundle 里有大量 `tts` 引用（153 次） |
| **判断** | **需进一步确认** Hana 是否有用户可配的 TTS 模型字段 |

Hana 侧 `tts` 出现 153 次，但没找到形如 `tts_model` 的 preferences 键。
**这条我标为不确定**——需要看 Hana 设置页有没有 TTS 配置入口才能定论。

---

### 4. LLM 直连配置 —— 【部分重复，P2】

| | |
|---|---|
| **位置** | `env_config.py:69 get_llm_config()` |
| **oc-pet 现在读** | `.env` 的 `LLM_*`，为空则回退 Hana catalog |
| **Hana 权威来源** | agent `config.yaml` 的 `models.chat` |
| **判断** | **设计上可接受**，但优先级反了 |

`.env` 优先本身是合理的（高级覆盖）。但**默认状态下**
用户改 Hana 的聊天模型，oc-pet 直连时可能仍用旧的 `.env` 值。
建议：`.env` 只作显式覆盖，空值时明确走 Hana。

---

### 5. 工具/插件/App 扫描 —— 【合理，已复用】

| | |
|---|---|
| **位置** | `core/hana_catalog.py` |
| **做法** | 优先调 Hana 本地 API（`/api/plugins`、`/api/apps`、`/api/agents`），失败回退静态扫描 |
| **判断** | **合理** |

这是 oc-pet 里**唯一做对的**一处：先问 Hana（权威），再自己扫（降级）。
`_read_server_info()` 还特意「每次都重读，token 每次重启会变」——动态性也对。

**这一处应该成为其他所有地方的模板。**

---

## 三、模式总结

```
做对的：hana_catalog.py   → 先问 Hana API，失败才自己扫
做错的：其余全部          → 自己读文件，不问 Hana 有没有权威来源
```

**三个共同特征**（今天的三个 bug 全部符合）：

1. **自己发明字段名**（`models.vision` vs Hana 的 `vision_model`）
2. **自己写路径解析**（`Path.home()/.hanako` vs `HANA_HOME`）
3. **只读一次**（`__init__` 里缓存 vs 每次读 / mtime 失效）

第 3 条今天也踩到了：`get_vision_config` 每次截屏都读（对），
`get_utility_config` 只在 `__init__` 读（错）——**同一项目里两条链行为不一致**。

---

## 四、建议的修复顺序

| 序 | 修什么 | 影响面 | 风险 | 状态 |
|---|---|---|---|---|
| 1 | `hanako_home()` helper + 全部替换 | 全部文件读取 | 低（默认值不变，行为等价） | ✅ 已修（25 处全改） |
| 2 | ASR 读 Hana `speechRecognition` | 语音识别 | 低 | ✅ 已修 |
| 3 | 统一动态性（所有配置用 mtime 或每次读） | 全部配置 | 中 | ✅ utility 已修，其余待查 |
| 4 | 确认 Hana 是否有 TTS 配置入口 | TTS | 待查 | ⏸ 未定论 |

> **修正**：第一节写「18 处」是 grep 漏了 `tts_provider/` 和 `ui/` 子目录，
> 实际全项目 **25 处**。已全部替换（见下）。

---

## 五、本次已修

### 1. `hanako_home()` helper（P0）—— 25 处全改

新增 `hanako_home.py`，提供：

```python
def hanako_home() -> Path:
    """HANA_HOME 环境变量优先，回退 ~/.hanako。"""
    env = os.environ.get("HANA_HOME", "").strip()
    return Path(env) if env else Path.home() / ".hanako"
```

已替换的模块（**25 处，全覆盖**）：

| 文件 | 处数 | 改法 |
|---|---|---|
| `env_config.py` | 5 | 函数调用 |
| `core/harness_adapter.py` | 3 | 函数调用 |
| `core/hanako_context.py` | 2 | 删模块级常量，改函数调用 |
| `pet_manager.py` | 1 | 常量值改 helper |
| `core/hana_catalog.py` | 1 | 同上 |
| `core/tool_executor.py` | 2 | 同上 |
| `core/tool_registry.py` | 2 | 同上 |
| `core/perception/schedule.py` | 1 | 同上（**被 `inspection.py` 跨模块引用，保留常量名**） |
| `core/startup_check.py` | 1 | 局部变量（注意避开自遮蔽） |
| `core/capability_registry.py` | 1 | 函数调用 |
| `core/conversation_engine.py` | 1 | 函数调用 |
| `pet.py` | 1 | 函数调用 |
| `ui/plugin_panel.py` | 2 | 常量值改 helper |
| `ui/character_card.py` | 1 | 常量值改 helper |
| `ui/settings_dialog.py` | 6 | 函数调用 |
| `tts_provider/` 5 个文件 | 5 | 常量值/返回值改 helper |

**保留常量名而非直接删**：`schedule.HANAKO_HOME` 被 `inspection.py`
跨模块引用，`tool_executor.HANAKO_DATA` 等同理。改成
`HANAKO_HOME = hanako_home()` 兼容所有引用点。

**防回归**：`test_hanako_home.py` 加两道哨兵——
① 已知 19 个模块逐个检查；② 全项目 rglob 扫描（能抓到新增文件）。
后者就是因为今天只查已知模块而漏了 `tts_provider/` 和 `ui/`。

### 2. ASR 读 Hana `speechRecognition`（P1）

`get_asr_api_config()` 优先级：`.env ASR_*` → Hana `speechRecognition.defaultModel` → 空。
`enabled=false` 视为未配置。

### 3. utility 配置动态刷新（P2）

`_refresh_utility_cfg()` 用 preferences.json 的 mtime 做失效检测，
用户改完 Hana 设置页立即生效（此前要重启桌宠）。
与 `screen.py` 的视觉配置行为对齐（那条是每次截屏都读）。

---

## 六、测试

| 新增测试 | 例数 | 钉死的不变式 |
|---|---|---|
| `test_hanako_home.py` | 6 | HANA_HOME 优先；空串回退；哨兵防写死 |
| `test_vision_preferences_source.py` | 6 | preferences 优先于 agent config |
| `test_utility_model_routing.py` | 15 | user 不切；内部来源切且还原；mtime 刷新 |
| `test_asr_preferences_source.py` | 6 | Hana speechRecognition 被读；.env 可覆盖 |

同时修正 3 个旧测试对已删除的 `HANAKO_HOME` 常量的依赖
（`test_ishiki_fallback.py`、`test_bugfix6_item_b_memory.py`、
`test_vision_config_chain.py`）。

全量：**1189 passed, 1 skipped**

---

## 七、追加发现：子 agent 与对话共用模型（2026-09-17 下午）

### 现象

派去执行本排查的子 agent（GLaDOS）在完成任务前失败。
会话文件最后三条记录：

```
stopReason: "error"
errorMessage: "429: inference exceeds tpm/rpm limit"
```

三次重试（07:16:54 → 07:17:18 → 07:17:47）均撞限流，然后放弃。

### 根因：与对话共用 `models.chat`

子 agent 会话的 `model_change` 记录：

```json
{"type":"model_change","provider":"日日新",
 "modelId":"sensenova-6.8-flash-lite"}
```

与 ophelia 的 `models.chat` 完全一致。

查 Hana bundle：**没有任何子 agent 专用模型字段**——
`subagent_model` / `subagentModel` / `subagent_role_model` 均 0 次命中。

**推断**（未直接验证）：子 agent 继承父 agent 的对话模型，无独立槽位。

### 影响

```
用户对话   ┐
子 agent   ├─→ 同一份 sensenova 配额
后台任务   ┘（已于今日摘走，见第五节）
```

即：**委派子 agent 会与用户对话抢配额**。今天这次失败是活证据——
子 agent 递归列目录（输出大量文件清单，上下文膨胀）+ 同时段用户对话，
两者叠加触发限流。

### 待办

- 验证「子 agent 继承父模型」这个推断（可用小任务看 `model_change`）
- 若属实，考虑：子 agent 是否也应走 `utility_model`，或至少给
  子 agent 加配额退避（当前重试 3 次即放弃，间隔太短）

---

## 八、附：本次排查的元教训

排查者死于被排查的病症。

这不是修辞——它说明**诊断行为本身会消耗被诊断的资源**。
委派子 agent 去查 429，子 agent 因 429 而死；
如果要继续用委派方式查配额问题，得先给子 agent 一条不抢配额的通道，
或者限制其读取量（本次失败的直接诱因之一是它一次列了整个目录树）。
