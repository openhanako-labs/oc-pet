"""确认 HANA_HOME 环境变量的语义：是用户可改的，还是内部注入的。"""
import io
import re

P = (
    r"C:\Users\Administrator\.hanako\artifacts\server"
    r"\0.999.2-win32-x64-527cfd4c87811ed9-g3b1a51b49bf9d59b\bundle\index.js"
)
t = io.open(P, encoding="utf-8", errors="replace").read()

print("=== 读 HANA_HOME 环境变量的地方 ===")
for m in list(re.finditer(r"process\.env\.HANA_HOME|env\.HANA_HOME", t))[:6]:
    seg = t[max(0, m.start() - 400) : m.start() + 500].replace("\n", " ")
    print("---")
    print(seg)
    print()

print("\n=== .hanako 目录是怎么拼出来的 ===")
for m in list(re.finditer(r'\.hanako["\'`]', t))[:5]:
    seg = t[max(0, m.start() - 300) : m.start() + 300].replace("\n", " ")
    print("---")
    print(seg)
