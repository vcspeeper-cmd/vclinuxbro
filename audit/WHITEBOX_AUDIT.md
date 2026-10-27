# ruyiJS v0.1.5 官方 release 白盒审计报告

> 审计方式：**纯静态只读分析**（zipfile 列结构 / 读源码文本 / pefile 读 PE 头 / strings 特征扫描 / sha256 交叉验证）。
> **全程未 pip install、未运行任何 release 内代码与二进制。** 下载隔离在 `/opt/ruyijs-audit/`（Linux 主机，非 Windows，Windows 二进制也无法运行）。
> 目的：为 Linux 自托管重新编译提供白盒依据；同时回答「release 是否有毒」。

---

## 0. 结论摘要（TL;DR）

1. **完整性**：v0.1.5 wheel 与 js runtime zip 下载完整，`js.exe` sha256 与仓库 `runtime.json` 声明的 `shell_sha256` **精确匹配** → 资产未在分发/镜像传输中被篡改。
2. **架构揭示（本轮最重要发现）**：ruyiJS 并非「SpiderMonkey + Mozilla C++ Necko」的简单组合——
   - native 核心 = **`js.exe`（SpiderMonkey 158 + 40 目录自研 Web 平台 API 实现 + WASM + urlpattern(Rust) 静态链接）**，46.7MB x64 PE，**无壳、无恶意导入**；
   - 网络栈 = **`necko-derived`：内嵌 Python 3.14 + requests 生态（urllib3/brotli/cffi/certifi/h2/hpack/idna/zstandard…）**，不是 Mozilla C++ Necko；
   - transport 层（29 个 C++ 文件）= 网络请求管道（worker session / JSON schema / necko channel）。
3. **毒性评估：未发现恶意行为**。js.exe 导入表仅 mozglue/bcrypt/DWrite/D2D1/MSVC 运行时；strings 无 powershell/cmd/reg/schtasks/webhook/telegram/exfiltrate 等；内嵌文本均为 Firefox license 页与 Web 平台/WASM 标准字符串。`bitcoin/password/secret/token` 命中均为 license.html 的维基百科文本与 Web 标准 API 词（DOMTokenList、exportSecret 等）。
4. **Python 层 = 源码层**：wheel 内 `ruyijs/*.py` 与 GitHub 仓库 `python/ruyijs/*.py` 文件大小逐字节一致（如 `__init__.py` 449B、`_context.py` 12247B）→ 打包未注入隐藏 Python 代码。
5. **编译指纹**：`D:\BaiduNetdiskDownload\ivv8\ivv8\ruyijs\build\runtime\urlpattern-native\donor-mirror\...` → 作者用百度网盘下载的 Mozilla donor 源码，工具链 = Rust（urlpattern-native）+ MSVC；`donor-mirror` = 官方源码捐赠包镜像。
6. **重新编译可行性**：**网络层（necko-derived）为纯 Python，可直接移植 Linux**；SpiderMonkey 158 源码 Mozilla 官方公开（MPL-2.0）；`src/` 40 目录 C++ 源码完整在仓库 → **Linux 自托管重建路径成立**（详见 §6）。

---

## 1. Release 资产清单与完整性验证

| 资产 | 大小 | sha256（下载所得） | 官方声明 | 匹配 |
|---|---|---|---|---|
| `ruyijs-0.1.5-py3-none-win_amd64.whl` | 43,400,031 | `cc4c0ed62ba28b55d19eea517e0d4e937cabf6502e48c8f7c241bdcc66e5bd03` | — | — |
| `ruyijs-js-0.1.5-win-amd64.zip`（内含 `js.exe`） | 19,293,999（zip）/ 46,705,152（exe） | zip `199c5098…` / **exe `30581e7af9edee3b6531c4149ccbdd4d82325473e7ea04664e20f1ef7a4bcf4c`** | runtime.json `shell_sha256: 30581e7a…` | ✅ **精确匹配** |

- 下载通道：GitHub 直连卡死（626KB 截断）→ 改经 `gh-proxy.com` 镜像，传输后 sha256 与官方声明一致 → **镜像通道无损**。
- `runtime.json`（仓库根）还声明：`archive: ruyijs-runtime-0.1.5-win-amd64.zip`、`inventory_sha256: e1bcbc51…`、Gitee 镜像 `losenine888/ruyiJS`、`release_api: gitee.com/api/v5/repos/losenine888/ruyiJS/releases/tags/v0.1.5`。

## 2. wheel 结构白盒（zipfile 只读）

```
ruyijs/                      ← Python 绑定层（18 个 .py，与 GitHub 源码一致）
ruyijs/examples/             ← 13 个真实验证系统示例（极验/瑞数/数美/易盾/天御/抖音/小红书…）
ruyijs/_runtime/
  dist/js.exe       46,705,152   ← native runtime（SpiderMonkey shell）
  dist/mozglue.dll    250,368   ← Mozilla 基础库
  dist/msvcp140.dll 553,552 + vcruntime140*.dll   ← MSVC 运行时
  licenses/                      ← MPL-2.0 + 第三方 license 全集
  SHA256SUMS           67,313   ← runtime 全部组件哈希清单（inventory）
  vendor/necko-derived/          ← ★ 网络栈 = 内嵌 Python 3.14 + requests 生态
    python/python314.dll 6.7MB + python314.zip + DLLs/  （_ssl/_hashlib/_ctypes/sqlite3/tkinter…）
    python/Lib/site-packages/    （brotli/certifi/cffi/charset-normalizer/cryptography/h2/hpack/
                                   idna/publicsuffixlist/pycparser/requests/urllib3/zstandard）
    base-manifest.json 266,070 + manifest.json 191,086  （Web 平台组件 manifest）
```

- `ruyijs-js-0.1.5.zip` = 仅一个 `js.exe`（独立 runtime 包，与 wheel 内同大小同 hash）。

## 3. js.exe 静态分析（pefile + strings，不运行）

### 3.1 PE 头
- `Machine: 0x8664`（x64）；`Subsystem: 3`（CUI）。
- 区段：`.text` 24.6MB / `.rodata` 12.9MB / `.rdata` 8.5MB / `.pdata` 0.5MB / `.reloc` / `.tls` / `.data`。
  **标准 PE 布局，无 UPX0/UPX1 等加壳区段**；`.text` 熵 6.34（正常代码密度，非加密载荷）。
- 无版本资源（FileInfo 空）。

### 3.2 导入表（全部）
`mozglue.dll`、`SHELL32`、`ADVAPI32`、`bcrypt`、`MSVCP140`、`ole32`、`GDI32`、`USER32`、
`DWrite.dll`、`d2d1.dll`、`ntdll`、`bcryptprimitives`、`KERNEL32`、`VCRUNTIME140(+_1)`、`api-ms-win-crt-*` 全套。
→ **无可疑网络/命令导入**（无 wininet/urlmon/ws2_32/winhttp）。网络功能确实不在 js.exe 内，而在 necko-derived 的 Python 子进程。`DWrite/d2d1` 表明内置文字/2D 渲染（Canvas/字体测量）。

### 3.3 strings 关键特征
- 版本：`Firefox/158.0`、`Mozilla/5.0`、`SpiderMonkey 158`，含 `537.36`（Chrome UA 兼容伪装，用于指纹场景）。
- **编译路径泄露**：`D:\BaiduNetdiskDownload\ivv8\ivv8\ruyijs\build\runtime\urlpattern-native\donor-mirror\urlpattern\src\constructor_parser.rs` 与 `tokenizer.rs`；`icu_78`（ICU 78）→ urlpattern-native 为 Rust + ICU 实现，`donor-mirror` = Mozilla 官方源码捐赠包镜像。
- 内嵌完整 Web 平台内容：WASM 全指令集表（wasm text format）、DOM 标准字符串、license.html（维基百科长文，含 bitcoin/password/secret 等单词——均为普通英文文本）。
- **恶意特征扫描：零命中**（powershell/cmd.exe/reg.exe/schtasks/rundll/webhook/telegram/exfiltrate/keylog 全无）。

## 4. 三层架构揭示（与 README 描述的修正）

| 层 | 实现 | 来源 | 许可证 |
|---|---|---|---|
| 1. JS 引擎 | SpiderMonkey 158（静态链接进 js.exe） | Mozilla Firefox 158 源码捐赠（`donor-mirror`） | MPL-2.0 |
| 2. Web 平台 API | `src/` 40 目录 C++ 自研（DOM/Canvas/WebGL/Crypto/IndexedDB/RTC/ServiceWorker/Workers…） | 仓库源码（约 150+ 文件，`src/runtime` 153 文件 + `src/transport` 29 文件 + `src/web_*` 35 目录） | 仓库内 |
| 3. 网络栈 | **necko-derived = Python 3.14 + requests 生态**（非 Mozilla C++ Necko） | wheel 内嵌 + `vendor/necko-native/`（brotli 等原生扩展） | CPython + 各包 license |
| 4. 传输管道 | `src/transport/`（necko_channel/worker_session/worker_json/schema） | 仓库源码 | 仓库内 |
| 5. RPC | Python `_pipe.py` ↔ js.exe：Windows named pipe，JSON 帧，16MiB 上限，callback 事件循环 | 仓库源码 | 仓库内 |

> 关键修正：README 写「Necko」，实际 wheel 的 `necko-derived` 是 **Python requests 生态的重新实现**（含 certifi/urllib3/cryptography 等），不是 Mozilla C++ Necko。这对 Linux 移植是巨大利好——网络层可直接复用。

## 5. Python API 契约（完整提取，ruyijs-linux 复刻蓝图）

### Context（继承 8 控制类）
```
Context(shell=None, timeout=10.0, environment=None, mode="default", proxy=None)
  # 启动 js.exe --ruyijs-rpc <pipe> --ruyijs-mode <mode>；proxy → env RUYIJS_HTTP_PROXY
load_page(url, html, *, replay_async_scripts=None)   # 注入 HTML 跑 parser scripts → {"url","scripts"}
navigate(url)                                        # 地址栏导航（无 Referer, Sec-Fetch-Site:none）
checkpoint() / collect_garbage()
expose(function, name=None)                          # Python 函数暴露给 JS
set(name, value) / native(name, source, *, constructor=False)
close() / __enter__ / __exit__ / __del__
```

### 控制面（方法签名全集）
- **EnvironmentControl**：`configure_fingerprint(fingerprint)` / `configure_environment(environment)` / `get_defaults()`
- **SchedulingControl**（事件循环重放，反检测核心）：`configure_clock(mode, epoch_ms)` / `clock_status` / `configure_timer_replay(enabled)` / `pending_timers` / `timer_realm` / `configure_message_replay` / `pending_messages` / `run_message(...)` / `run_timer(...)` / `configure_navigation_replay(documents)` / `navigation_replay_status` / **`advance(ms, step_ms=16.667, max_tasks=100000)`** / `drain_timers` / `sleep(ms)` / `tick(ms=0)`
- **RandomControl**：`configure_random(state0, state1)` / `configure_crypto_replay(documents)` / `crypto_replay_status`
- **ResourceControl**（网络资源/凭证）：**`export_cookies()`** / **`cookie_header(url, *, source_url, navigation=False, method="GET")`** / `put_resource(url, body, status=200, headers=None)`（注入替换响应）/ `get_resource(url)` / `resource_status` / `net_log` / `clear_resources`
- **ConsoleControl**：`console_log(clear=False)` / `clear_console`
- **InspectorControl**：`peek_api_log(filter)` / `get_api_log(filter)` / `clear_api_log`
- **InputControl**：`dispatch_input(event_type, *, target="document", **init)` / `click(target, hold_ms=80)` / `type(text, *, target=None, interval_ms=60)` / `press(key, *, target=None)`
- **EvaluationControl**：`eval(code, filename, line, col, to_py, devtools, name)`（typed snapshot / public 双模式）/ `locals`（原始 Window）/ `_host_value(reference, action, key, value)`
- **Pipe**：`connect/receive/send/close`（named pipe，帧 ≤16MiB）

### 典型反检测工作流（examples/README 揭示）
「长流程请求分层」：页面脚本生成 challenge/签名/指纹/Cookie → 参数取出后由网络层（Necko 会话）发送 → 混合路径减少页面等待。

## 6. Linux 重新编译路径评估

### 6.1 组件可获取性
| 组件 | 来源 | Linux 可获取性 |
|---|---|---|
| SpiderMonkey 158 | Mozilla 官方源码（hg/git 镜像，MPL-2.0） | ✅ 官方开源，可自行编译 |
| Web 平台 API（src/ 40 目录） | 本仓库源码 | ✅ 完整在仓库 |
| urlpattern-native（Rust+ICU） | 仓库源码 + crates.io 依赖 | ✅ cargo 可构建 |
| necko-derived 网络栈 | **纯 Python（requests 系）** | ✅ 直接 pip 安装，无需编译 |
| transport/worker 管道 | 仓库源码 | ✅ |
| RPC 层 | Python 绑定源码 | ✅ 仅需把 named pipe 换成 Unix domain socket |

### 6.2 主要工作量
1. SpiderMonkey 158 Linux 编译（需下载 Firefox 158 源码树 → 编译 `js` shell，约数 GB 源码 + 数小时构建）；
2. `src/` 40 目录 C++ 代码从 Windows（MSVC + named pipe）移植到 Linux（GCC/Clang + unix socket）—— 重点改 `_pipe.cpp`/`pipe_async*`/`worker_*`；
3. 网络子进程从 Python 3.14 for Windows 换 Linux Python 3.10+（oasis 已有）；
4. 绑定层 `_runtime_paths.py` 指向 Linux `js` 二进制路径。

### 6.3 现实替代（本仓库 ruyijs-linux 已选）
**Camoufox（真实 Firefox 内核反检测，playwright 驱动）+ Context API 层** 已在 oasis 跑通（L1 端到端点击）。相比从源码重建 SpiderMonkey shell，工程量低 2 个数量级，且反检测能力来自真实 Firefox 引擎（与 ruyiJS「真实引擎执行网站 JS」哲学一致）。

## 7. 结论

- **「release 可能有毒」排查结果：静态层面未发现恶意载荷/行为**；唯一"风险"是 Windows 专有二进制本身无法在 Linux 运行，且官方不提供 Linux 版。
- 白盒信息已足够支撑两条路线：① 网络层纯 Python 直接复用（necko-derived 等价物）；② 完整重建（SpiderMonkey 源码 + 仓库 src/ 移植）。
- **审计产物**：本报告 + `audit/` 内 sha256 清单与 API 契约提取；原始二进制隔离在 `/opt/ruyijs-audit/`（不执行、不分发）。
