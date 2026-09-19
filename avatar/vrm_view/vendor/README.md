# vendor/ — VRM 视图的三方 JS（本地副本）

这里放的是 `index.html` 依赖的三方库，**本地副本**，因此桌宠离线也能渲染 VRM。

| 文件 | 包 | 版本 | 许可 |
|---|---|---|---|
| `three.core.min.js` | three | 0.180.0 | MIT |
| `three.module.min.js` | three | 0.180.0 | MIT |
| `three-vrm.module.js` | @pixiv/three-vrm | 3.5.5 | MIT |
| `addons/loaders/GLTFLoader.js` | three（`examples/jsm`） | 0.180.0 | MIT |
| `addons/utils/BufferGeometryUtils.js` | three（`examples/jsm`） | 0.180.0 | MIT |

## 依赖关系（改动前先读）

- `three.module.min.js` 内部 `import "./three.core.min.js"` —— three r167 起把核心拆了出去，
  **两个文件必须同目录**，缺一个就整页白屏。
- `three-vrm.module.js` 只裸导入 `"three"`，靠 `index.html` 里的 **import map** 指到
  `three.module.min.js`。它把 `@pixiv/three-vrm-core`、`-springbone`、`-materials-mtoon`
  等子包都打包在自己内部，所以只需要这一个文件。
- `GLTFLoader.js` 只依赖 `../utils/BufferGeometryUtils.js`（**相对路径**，所以
  `addons/` 下的目录结构必须保留）。
- `VRMLoaderPlugin` 需要 `GLTFLoader` 从外部注册，three-vrm 自己不引入它。

## 刷新方式

在项目根目录执行（PowerShell，用 `curl.exe` 走系统证书；`Invoke-WebRequest` 亦可）：

```powershell
$T = "0.180.0"; $V = "3.5.5"
$d = "avatar/vrm_view/vendor"
New-Item -ItemType Directory -Force -Path "$d/addons/loaders", "$d/addons/utils" | Out-Null
curl.exe -sSL -o "$d/three.core.min.js"   "https://unpkg.com/three@$T/build/three.core.min.js"
curl.exe -sSL -o "$d/three.module.min.js" "https://unpkg.com/three@$T/build/three.module.min.js"
curl.exe -sSL -o "$d/three-vrm.module.js" "https://unpkg.com/@pixiv/three-vrm@$V/lib/three-vrm.module.js"
curl.exe -sSL -o "$d/addons/loaders/GLTFLoader.js" "https://unpkg.com/three@$T/examples/jsm/loaders/GLTFLoader.js"
curl.exe -sSL -o "$d/addons/utils/BufferGeometryUtils.js" "https://unpkg.com/three@$T/examples/jsm/utils/BufferGeometryUtils.js"
```

改版本时的两点注意：

1. `@pixiv/three-vrm` 的 `peerDependencies` 要求 `three >= 0.137`，但它的 devDependencies
   通常跟着某个较新的 three 走；**跨大版本升级 three 后务必跑一次**
   `python tools/verify_vrm_view.py`（会用样片模型真渲染并截图）。
2. 升级后如果页面报 `does not provide an export named …`，多半是 three 的内部拆包文件名
   变了（`three.core*.js`）——去 `https://unpkg.com/three@<版本>/build/?meta` 看实际文件名。
