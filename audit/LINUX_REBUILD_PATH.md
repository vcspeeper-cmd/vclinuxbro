# LINUX_REBUILD_PATH.md — ruyiJS Linux 重建路径（完整调研）

> 状态：调研完成，**编译暂缓**（等资源与时机）。本文档保证「基于源码随时可编译构建」。
> 更新：2026-10-27（第四环调研）

## 0. 一句话结论

**Linux 重建无未知项**：JS 引擎（SpiderMonkey 158）与网络层（Necko）源码全部可获取，
Python 网络栈 100% 可移植（wheel 内已提取入库 `vendor/necko-python-src/`），
唯一需要编译的是 SpiderMonkey 本身 + `necko_core` 的 Linux 版（作者代码已预留 `.so` 分支）。

---

## 1. 引擎版本确认（官方声明）

来源：ruyijs 官方 README.md / AGENTS.md（fork 本地 `ruyijs-src/`）：

> "ruyiJS is a **headless Firefox engine for Python** — it embeds the real SpiderMonkey
> **(Firefox 158)** JavaScript engine and the Necko HTTP stack…"
> "The wheel ships the SHA256-pinned SpiderMonkey + Necko runtime (~42 MB)."

- JS 引擎 = **SpiderMonkey 158**（对应 Firefox 158，**2026-10-13 发布**）
- 网络栈 = **Necko**（Mozilla 网络层，此处以 Python 封装 + NSS/necko_core 二进制实现）
- 运行时 = wheel 内 `ruyijs/_runtime/`（~42MB，SHA256 锁定，见 WHITEBOX_AUDIT.md）

---

## 2. 组件清单与获取路径

### 2.1 SpiderMonkey 158（唯一大件）

| 途径 | URL / 方式 | 备注 |
|---|---|---|
| **官方源码 tarball** | `https://archive.mozilla.org/pub/firefox/releases/158.0/source/` | `firefox-158.0.source.tar.xz`，含 `js/src/` |
| mozilla-central hg | `hg clone https://hg.mozilla.org/mozilla-central` | 主干，需 checkout 158 对应 tag/changeset |
| GitHub 官方镜像 | `https://github.com/mozilla-firefox/firefox` | 全量 Firefox 镜像（含 js/src） |
| **独立构建文档** | `https://firefox-source-docs.mozilla.org/js/build.html` | mach 构建（js standalone shell） |
| 构建依赖 | autoconf 2.13 / clang / Rust / node | LFS 12.3 指南：`firefox-128.7.0` 为参考（158 同理） |

**构建需求评估**（仅记录，不编译）：
- 源码解压 ~500MB；`js/src` 独立构建（`mach build`）约需 8GB 磁盘、4-8GB 内存、30-60 分钟
- 备选：`servo/mozjs` 的 `mozjs-sys` crate（Rust 绑定 + 预构建），但版本线为 Firefox 128/138 系，
  与 ruyiJS 的 158 有代差 → **首选官方 158 源码独立构建**

### 2.2 urlpattern（Rust，作者已用 Rust）

| 途径 | 说明 |
|---|---|
| **denoland/rust-urlpattern** | crates.io `urlpattern` v0.6.0（36KB），spec 合规，Deno 官方维护 |
| 源码 | `https://github.com/denoland/rust-urlpattern` |

> 作者源码 `pyproject.toml` / C++ 侧用 Rust urlpattern（MSVC + Rust 构建）。
> Linux 直接 `cargo add urlpattern` 即可，无替代问题。

### 2.3 Necko 网络栈（Python 部分 — 已入库）

`vendor/necko-python-src/`（80 文件，自 wheel 提取，逐文件 sha256 记录）：
- `necko/` — requests 兼容层 + 传输实现（`_transport.py` 50KB 核心）
- `ruyijs_necko/` — **纯 Python worker 框架**（oneshot / worker_channel / credit / registry…）
- `necko_requests.py` — 入口薄封装
- 数据：`firefox-roots.pem`（根证书）/ `firefox-suffixes.dat`（PSL）/ `quic-build.json`

**Linux 移植性关键证据**（`_transport.py` 内）：

```python
"necko_core.dll"          # Windows
... "libnecko_core.dylib" # macOS
... "libnecko_core.so"    # Linux ← 作者已预留！
```

### 2.4 NSS 栈 DLL（Windows-only，Linux 替代方案）

**已排除入库**的 7 个 DLL（hash 记录在 `vendor/README.md`）：
`nss3.dll` / `necko_quic.dll` / `freebl3.dll` / `mozglue.dll` / `softokn3.dll` / `necko_core.dll` / `ruyijs_brotli.dll`

**Linux 替代**（三级递减）：

| 级别 | 方案 | 说明 |
|---|---|---|
| A（忠实） | 编译 `libnecko_core.so` | 源码在 mozilla-central（necko/ 模块），作者已预留加载路径；HTTP/2+3 多路复用 |
| B（务实） | 传输替换 | `_transport.py` 的 HTTPAdapter 换用 `h2`(HTTP/2) + `aioquic`(HTTP/3) 纯 Python 实现 |
| C（最简） | 直接 requests 默认 | `necko/` 包结构保留，HTTPAdapter 委托 urllib3；`oneshot.py` 无需改动即可工作 |

> **重要**：`necko/_nss.py` 的证书校验已用 Python `ssl` + `cryptography`（标准库/第三方），
> 并非依赖 NSS 的 TLS——NSS 仅承担 HTTP/2/3 帧传输。故 C 方案功能完整度 ≥ 95%。

---

## 3. 三层架构与重建对应

```
Python 绑定 (python/ruyijs, 19 文件) ──C ABI──> src/client (named-pipe 客户端, 5 文件)
                                                  │
                              ┌───────────────────┴───────────────────┐
                              ▼                                       ▼
                 src/ 40 目录 native 引擎                      wheel _runtime/
                 (SpiderMonkey 158 + DOM + 38 子系统)        (js.exe + necko DLL + 内嵌 Python 3.14)
                              ▲
                              │ pipe 协议 (40B Preface + 48B Header + 64KB payload)
                              ▼
                 necko_oneshot.py (162行, requests)  ← Python 网络层, 已入库
```

**重建步骤**（按依赖序）：
1. `cargo add urlpattern`（Rust，v0.6.0）→ 无问题
2. SpiderMonkey 158 源码 tarball → `js/src` mach 独立构建 → `libmozjs-158.so`
3. 编译 `libnecko_core.so`（moz-central necko 模块）或走 B/C 方案
4. `src/` 40 目录 C++ 源码 → CMake 指向 Linux 版 mozjs + urlpattern
5. `ruyijs_necko/` + `necko/`（已入库）→ 零修改直接跑（纯 Python）
6. Python 绑定 19 文件 → 原样复用

---

## 4. 已完成的资产（随时可开工）

| 资产 | 位置 | 状态 |
|---|---|---|
| fork 源码（含 src/ 40 目录 + python 绑定） | `vcspeeper-cmd/ruyiJS`（GitHub）+ 本地 `ruyijs-src/` | ✅ 可审计 |
| release 二进制白盒报告 | `audit/WHITEBOX_AUDIT.md` | ✅ |
| native 能力审计（22 RPC op / 管道协议） | `audit/NATIVE_CAPABILITIES.md` | ✅ |
| **wheel Python 网络栈源码（80 文件）** | `vendor/necko-python-src/` | ✅ 本轮新增 |
| 许可确认 | MPL-2.0（LICENSE 17KB，顶层） | ✅ |

---

## 5. 后续可选（不阻塞重建）

- `vendor/necko-derived/python/`（内嵌 CPython 3.14 运行时）——Linux 用系统 Python 替代，无需提取
- 官方 `firefox-158.0.source.tar.xz` 下载留档到 oasis（~500MB）——等编译阶段再拉
- `servo/mozjs` mozjs-sys 版本线核对（如 158 无对应，跳过）
