r"""Hana 数据目录解析 —— 与 Hana 官方逻辑保持一致。

## 为什么需要这个模块

oc-pet 有 18 处读 Hana 数据，全部写死 `Path.home() / ".hanako"`。
但 Hana 官方的解析规则是 **`HANA_HOME` 环境变量优先**：

```js
// Hana bundle 里的官方逻辑
function r({ envValue: d, packaged: h, homeDir: p = t.homedir() } = {}) {
  if (d && typeof d === "string")
    return i(d, p);                          // ← HANA_HOME 优先
  if (h)
    return e.resolve(e.join(p, ".hanako"));  // ← 没设才用默认
  throw new Error("HANA_HOME is not set. ...");
}
```

写死 `~/.hanako` 的后果：用户把 `HANA_HOME` 指到别处（多开、测试环境、
换盘符）时，oc-pet 会去读**旧目录或空目录**，全部配置静默失效。

当前本机 `HANA_HOME` 恰好等于默认值（`C:\Users\Administrator\.hanako`），
所以今天没暴露——但这是巧合，不是正确性。

## 用法

    from hanako_home import hanako_home
    cfg = hanako_home() / "agents" / agent_id / "config.yaml"

需要子目录时优先用 `hanako_path("agents", agent_id)`。

## 与 hana_catalog.py 的关系

`core/hana_catalog.py` 里也有 `HANAKO_HOME` 常量——它是模块级常量，
import 时就固化了。本模块的函数是**每次调用都解析**，能感知运行期
环境变量变化（虽然实际中 `HANA_HOME` 不会变，但动态解析没有代价）。
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["hanako_home", "hanako_path"]


def hanako_home() -> Path:
    """Hana 数据目录：`HANA_HOME` 环境变量优先，回退 `~/.hanako`。

    与 Hana 官方解析逻辑一致。空字符串视为未设置（与 Hana 的
    `if (d && typeof d == "string")` 判断等价——空串是 falsy）。

    Returns:
        Hana 数据目录的绝对路径（不做存在性检查）。
    """
    env = os.environ.get("HANA_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".hanako"


def hanako_path(*parts: str) -> Path:
    """`hanako_home()` 下拼接子路径。

    Args:
        *parts: 相对路径片段，如 `hanako_path("agents", "ophelia", "config.yaml")`

    Returns:
        拼接后的路径。
    """
    p = hanako_home()
    for part in parts:
        p = p / part
    return p
