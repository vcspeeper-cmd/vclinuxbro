# ruyijs-linux

**ruyiJS 的 Linux 自托管开源实现** —— 真实浏览器引擎执行网站 JS，隔离 Context API，纯开源组件。

> **安全声明**：官方 ruyiJS 仅发布 Windows x64 wheel（native runtime = SpiderMonkey 158，闭源且不提供 Linux 版）。
> 本项目**不下载、不运行官方 release 二进制**（其可能含未知风险），而是以「源码审计 + 开源引擎」方式自托管。
> 官方 release 的白盒审计报告见 [`audit/WHITEBOX_AUDIT.md`](audit/WHITEBOX_AUDIT.md)（纯静态分析，未运行）。
> Native 能力审计报告见 [`audit/NATIVE_CAPABILITIES.md`](audit/NATIVE_CAPABILITIES.md)（三层架构 / 22 个 RPC op / Necko 管道协议 / 网络栈源码清单 / Linux 重建组件映射）。
> **许可修正**：GitHub API 显示 NOASSERTION 是误判，仓库顶层实际为 **MPL-2.0**（Mozilla Public License 2.0，弱 copyleft，允许再分发/修改）。
> Linux 重建路径（完整调研，编译暂缓）见 [`audit/LINUX_REBUILD_PATH.md`](audit/LINUX_REBUILD_PATH.md)（SpiderMonkey 158 源码获取 / urlpattern / NSS→OpenSSL 替代 / 组件清单）。
> wheel 内 Python 网络栈源码（80 文件，含 `ruyijs_necko/` 纯 Python worker 框架 + `necko/` 传输层）已提取入库 [`vendor/necko-python-src/`](vendor/necko-python-src/)。
> ruyiJS 官方 Python 绑定源码（`python/ruyijs` 20 文件 + `src/client` C ABI 5 文件，未改字节、sha256 留档）已 vendor 入库 [`vendor/ruyijs-binding-src/`](vendor/ruyijs-binding-src/)。
> **能力等价（第六环）**：仅凭本仓库源码即可在 Linux 上实现官方 ruyiJS 的全部能力——Context 核心 + 9 大控制面
> （Environment / Scheduling / Random / Resource / Console / Inspector / Input / Evaluation）+ 确定性重放 + 离线加载 +
> 纯 Python 网络替代通道，不依赖 Windows 仓库任何闭源运行时。官方 API 契约 48 方法全绿（`tests/smoke_test.py`）。

## 为什么做这个

ruyiJS（`martinkeith700/ruyiJS`，已 fork 到 `vcspeeper-cmd/ruyiJS` 供审计）能用 SpiderMonkey 执行网站自身 JS，
击败一批验证系统（瑞数/极验/天御/数美/易盾/Akamai/阿里 234…）——它们的共性 = **验证逻辑在 JS 里**。
但官方 runtime：

- 仅 Windows x64；
- native 二进制（js.exe）不随源码开源（SpiderMonkey 源码 Mozilla 开源，但 ruyiJS 自研的 40 目录 Web 平台 API 需自编译）。

本项目用 **Camoufox（真实 Firefox 内核反检测浏览器，playwright 驱动）** 作为引擎，在 Linux 上复刻 ruyiJS 的
「真实引擎执行网站 JS + 隔离上下文 + 独立代理」能力，且**全链路开源可审计**。

## 架构对照（ruyiJS vs ruyijs-linux）

| 层           | ruyiJS（官方）                                          | ruyijs-linux（本项目）                                                                         |
| ------------ | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| JS 引擎      | SpiderMonkey 158（MPL-2.0，源码需自编译）               | Camoufox = 真实 Firefox 内核（MPL-2.0，`pip install camoufox` 即可）                           |
| Web 平台 API | 自研 C++（`src/` 40 目录，Windows 专有）                | Firefox 原生（完整 DOM/Canvas/WebGL/Crypto/Workers…）                                          |
| 网络栈       | necko-derived（内嵌 Python 3.14 + requests 系）         | Camoufox 内置网络栈 + 浏览器内 fetch + 纯 Python 替代通道（necko 优先，缺 NSS 自动回退标准库） |
| 进程模型     | 每 Context 一个 js.exe 子进程（named pipe RPC）         | 每 Context 一个隔离 browser context（独立 cookie/存储/代理）                                   |
| 指纹控制     | `configure_fingerprint` / `configure_random` / 时钟重放 | Camoufox 反检测伪装（真实 Firefox 指纹）+ 每上下文隔离                                         |
| 请求拦截     | `put_resource` / `cookie_header`                        | `intercept()` 路由拦截 / `cookie_header()` 导出 Cookie 头                                      |
| 输入模拟     | `click` / `type` / `press`                              | `click` / `type` / `press`                                                                     |
| JS 执行      | `eval` / `set` / `expose` / `native`                    | `run_js` / `set` / `expose` / `native`                                                         |

## 能力对照（ruyiJS API → 本实现）

官方 `Context` = 核心方法 + 9 个控制面 mixin（`vendor/ruyijs-binding-src/python-ruyijs/`）。下表逐面对齐，共 48 个公开方法，`tests/smoke_test.py` 自动校验契约完整性。

| ruyiJS 控制面 / API                                                         | 语义                  | ruyijs-linux 对应                                                           |
| --------------------------------------------------------------------------- | --------------------- | --------------------------------------------------------------------------- |
| **Context 核心**                                                            |                       |                                                                             |
| `Context(shell, timeout, environment, mode, proxy)`                         | 隔离进程 + 独立代理   | `Context(proxy=..., headless=..., shell=..., environment=..., mode=...)`    |
| `load_page(url, html)` / `navigate(url)`                                    | 页面加载/导航         | `load_page(url, html)`（离线注入 HTML，不请求网络）/ `navigate(url)`        |
| `checkpoint()` / `collect_garbage()`                                        | 快照 / GC             | `checkpoint()` / `collect_garbage()`                                        |
| `expose(fn, name)` / `set(name, value)`                                     | Python↔JS 桥 / 设全局 | `expose(name, fn)` / `set(name, value)`                                     |
| `native(name, source)` / `eval(code, ...)`                                  | 注入 JS 定义 / 执行   | `native(source)` / `run_js(js)` / `eval(js)`（mode/shell 参数对齐）         |
| `close()` / `closed`                                                        | 生命周期              | `close()` / `closed` 属性                                                   |
| **EnvironmentControl**                                                      |                       |                                                                             |
| `configure_fingerprint(f)`                                                  | 指纹控制              | `configure_fingerprint(f)`（Camoufox 反检测 + 注入确定性指纹）              |
| `configure_environment(e)` / `get_defaults()`                               | 环境注入 / 默认值     | `configure_environment(e)` / `get_defaults()`                               |
| **SchedulingControl**                                                       |                       |                                                                             |
| `configure_clock(c)` / `clock_status()`                                     | 时钟重放              | `configure_clock(c)` / `clock_status()`（逻辑/虚拟时钟注入）                |
| `configure_timer_replay(...)` / `pending_timers()` / `run_timer(...)`       | 定时器重放            | `configure_timer_replay(...)` / `pending_timers()` / `run_timer(...)`       |
| `configure_message_replay(...)` / `run_message(...)` / `pending_messages()` | 消息重放              | `configure_message_replay(...)` / `run_message(...)` / `pending_messages()` |
| `configure_navigation_replay(...)` / `navigation_replay_status()`           | 导航重放              | `configure_navigation_replay(...)` / `navigation_replay_status()`           |
| `advance(ms)` / `drain_timers()` / `sleep(ms)` / `tick(ms)`                 | 事件循环推进          | `advance(ms)` / `drain_timers()` / `sleep(ms)` / `tick(ms)`                 |
| **RandomControl**                                                           |                       |                                                                             |
| `configure_random(r)`                                                       | 确定性 RNG            | `configure_random(r)`（注入 xorshift128p 确定性 RNG）                       |
| `configure_crypto_replay(...)` / `crypto_replay_status()`                   | crypto 重放           | `configure_crypto_replay(...)` / `crypto_replay_status()`                   |
| **ResourceControl**                                                         |                       |                                                                             |
| `export_cookies()` / `cookie_header(url)`                                   | 导出凭证              | `export_cookies()` / `cookie_header(url)`                                   |
| `put_resource(url, body, status)` / `get_resource(url)`                     | 请求拦截/注入         | `put_resource(url, body, status)` / `get_resource(url)`                     |
| `resource_status()` / `net_log()` / `clear_resources()`                     | 资源/网络日志         | `resource_status()` / `net_log()` / `clear_resources()`                     |
| **ConsoleControl**                                                          |                       |                                                                             |
| `console_log()` / `clear_console()`                                         | console 日志          | `console_log()` / `clear_console()`                                         |
| **InspectorControl**（debug mode）                                          |                       |                                                                             |
| `peek_api_log()` / `get_api_log()` / `clear_api_log()`                      | API 日志检查器        | `peek_api_log()` / `get_api_log()` / `clear_api_log()`                      |
| **InputControl**                                                            |                       |                                                                             |
| `dispatch_input(...)` / `click` / `type` / `press`                          | 输入模拟              | `dispatch_input(...)` / `click` / `type` / `press`                          |
| **EvaluationControl**                                                       |                       |                                                                             |
| `eval(code, mode, shell)` / `locals()`                                      | 求值选项              | `eval(js)` / `locals()`                                                     |

**网络替代通道**：`fetch(url, engine="necko")` 走纯 Python 通道——优先导入 `vendor/necko-python-src/necko_requests.py`（requests 兼容、NSS 加速）；Linux 缺 NSS 替代层时自动回退 `_stdlib_fetch`（`urllib.request` 纯标准库，零依赖）。两条通道均不依赖 Windows 仓库任何组件。

## 安装

```bash
# 方式一：从仓库安装（自动拉取 camoufox 依赖）
pip install git+https://github.com/vcspeeper-cmd/vclinuxbro.git

# 方式二：本地源码直接使用
pip install -e .          # 或 pip install camoufox 后把本目录加入 PYTHONPATH
camoufox fetch            # 下载真实 Firefox 反检测构建（约 1.3GB，可配 gh-proxy 镜像加速）
```

安装后提供 `ruyijs-linux` 命令行入口（等价 `python -m ruyijs_linux`）。

## 用法

```python
from ruyijs_linux import Context

with Context(proxy="http://127.0.0.1:7890") as ctx:
    ctx.open("https://example.com/login")                # 打开页面，等 JS 执行
    ctx.type("user@example.com", "#email")               # 模拟输入
    ctx.type("password123", "#password")
    ctx.click("button[type=submit]")
    ctx.advance(2000)                                    # 推进事件循环等验证
    token = ctx.run_js("window._token || document.body.innerText")
    print("token:", token)
    print("cookie:", ctx.cookie_header("https://example.com"))  # 复用登录态
```

CLI 一行式：

```bash
python ruyijs_linux.py https://example.com "document.title"
```

## 验证场景工作流（对齐 ruyiJS「长流程请求分层」）

1. 页面内跑网站自身 JS → 生成 challenge/签名/指纹/Cookie；
2. `run_js` 取出参数；
3. 用 `cookie_header()` + requests（或浏览器内 `fetch()`）发后续请求——页面不再承担纯网络等待。

## 目录

```
ruyijs_linux.py            Context API 实现（引擎：Camoufox）
pyproject.toml             pip 打包元数据 / 依赖 / ruyijs-linux 命令行入口
LICENSE                    MIT（自研代码）
tests/smoke_test.py        自检脚本：语法编译 + Context API 契约（48 方法 / 9 控制面）+ 网络替代通道本机实测 + 示例编译（--full 需 camoufox）
audit/WHITEBOX_AUDIT.md    官方 release 白盒审计报告（sha256 验证 / PE 分析 / 架构揭示 / 重建路径）
audit/NATIVE_CAPABILITIES.md native 能力审计（三层架构 / RPC op 全集 / Necko 管道协议 / 网络栈源码 / 重建映射）
audit/LINUX_REBUILD_PATH.md Linux 重建路径调研（SpiderMonkey 158 / urlpattern / NSS→OpenSSL / 组件清单）
vendor/necko-python-src/   wheel 内 Python 网络栈源码（80 文件，ruyijs_necko 纯 Python + necko 传输层，含逐文件 sha256）
vendor/ruyijs-binding-src/ 官方 Python 绑定 + C ABI 源码（25 文件，未改字节，sha256 留档）
vendor/README.md           vendor 来源/排除项/重建含义说明
examples/verify_demo.py    模拟验证系统示例（加载→执行 JS→提取 token→点击）
```

## 测试

```bash
python tests/smoke_test.py        # 无浏览器依赖：语法编译 + API 契约（48 方法/9 控制面）+ 网络通道实测 + 示例编译
python tests/smoke_test.py --full # 需 camoufox fetch 后：真实启动浏览器做端到端冒烟
```

## 许可证与致谢

- 本项目代码：MIT（自研）。
- 引擎：Camoufox（Firefox 内核，MPL-2.0）。
- 设计参考：ruyiJS（MPL-2.0，见 `audit/WHITEBOX_AUDIT.md` 对其 release 的审计）。
