# ruyijs-linux

**ruyiJS 的 Linux 自托管开源实现** —— 真实浏览器引擎执行网站 JS，隔离 Context API，纯开源组件。

> **安全声明**：官方 ruyiJS 仅发布 Windows x64 wheel（native runtime = SpiderMonkey 158，闭源且不提供 Linux 版）。
> 本项目**不下载、不运行官方 release 二进制**（其可能含未知风险），而是以「源码审计 + 开源引擎」方式自托管。
> 官方 release 的白盒审计报告见 [`audit/WHITEBOX_AUDIT.md`](audit/WHITEBOX_AUDIT.md)（纯静态分析，未运行）。
> Native 能力审计报告见 [`audit/NATIVE_CAPABILITIES.md`](audit/NATIVE_CAPABILITIES.md)（三层架构 / 22 个 RPC op / Necko 管道协议 / 网络栈源码清单 / Linux 重建组件映射）。
> **许可修正**：GitHub API 显示 NOASSERTION 是误判，仓库顶层实际为 **MPL-2.0**（Mozilla Public License 2.0，弱 copyleft，允许再分发/修改）。

## 为什么做这个

ruyiJS（`martinkeith700/ruyiJS`，已 fork 到 `vcspeeper-cmd/ruyiJS` 供审计）能用 SpiderMonkey 执行网站自身 JS，
击败一批验证系统（瑞数/极验/天御/数美/易盾/Akamai/阿里 234…）——它们的共性 = **验证逻辑在 JS 里**。
但官方 runtime：

- 仅 Windows x64；
- native 二进制（js.exe）不随源码开源（SpiderMonkey 源码 Mozilla 开源，但 ruyiJS 自研的 40 目录 Web 平台 API 需自编译）。

本项目用 **Camoufox（真实 Firefox 内核反检测浏览器，playwright 驱动）** 作为引擎，在 Linux 上复刻 ruyiJS 的
「真实引擎执行网站 JS + 隔离上下文 + 独立代理」能力，且**全链路开源可审计**。

## 架构对照（ruyiJS vs ruyijs-linux）

| 层           | ruyiJS（官方）                                          | ruyijs-linux（本项目）                                               |
| ------------ | ------------------------------------------------------- | -------------------------------------------------------------------- |
| JS 引擎      | SpiderMonkey 158（MPL-2.0，源码需自编译）               | Camoufox = 真实 Firefox 内核（MPL-2.0，`pip install camoufox` 即可） |
| Web 平台 API | 自研 C++（`src/` 40 目录，Windows 专有）                | Firefox 原生（完整 DOM/Canvas/WebGL/Crypto/Workers…）                |
| 网络栈       | necko-derived（内嵌 Python 3.14 + requests 系）         | Camoufox 内置网络栈 + 浏览器内 fetch                                 |
| 进程模型     | 每 Context 一个 js.exe 子进程（named pipe RPC）         | 每 Context 一个隔离 browser context（独立 cookie/存储/代理）         |
| 指纹控制     | `configure_fingerprint` / `configure_random` / 时钟重放 | Camoufox 反检测伪装（真实 Firefox 指纹）+ 每上下文隔离               |
| 请求拦截     | `put_resource` / `cookie_header`                        | `intercept()` 路由拦截 / `cookie_header()` 导出 Cookie 头            |
| 输入模拟     | `click` / `type` / `press`                              | `click` / `type` / `press`                                           |
| JS 执行      | `eval` / `set` / `expose` / `native`                    | `run_js` / `set` / `expose` / `native`                               |

## 能力对照（ruyiJS API → 本实现）

| ruyiJS API                                          | 语义                 | ruyijs-linux 对应                   |
| --------------------------------------------------- | -------------------- | ----------------------------------- |
| `Context(shell, timeout, environment, mode, proxy)` | 隔离进程 + 独立代理  | `Context(proxy=..., headless=...)`  |
| `load_page(url, html)` / `navigate(url)`            | 页面加载/导航        | `open(url)`                         |
| `eval(code, ...)`                                   | 执行 JS 取结果       | `run_js(js)`                        |
| `set(name, value)`                                  | 设 JS 全局           | `set(name, value)`                  |
| `expose(fn, name)`                                  | Python 函数暴露给 JS | `expose(name, fn)`                  |
| `native(name, source)`                              | 注入 JS 定义         | `native(source)`                    |
| `advance(ms)`                                       | 推进事件循环         | `advance(ms)`                       |
| `click` / `type` / `press`                          | 输入模拟             | `click` / `type` / `press`          |
| `export_cookies` / `cookie_header(url)`             | 导出凭证             | `cookies()` / `cookie_header(url)`  |
| `put_resource(url, body, status)`                   | 请求拦截/注入        | `intercept(pattern, reply, status)` |
| `console_log()`                                     | 取 console           | `console_log()`                     |
| `net_log()` / `api_log()`                           | 网络/API 日志        | `console_log()` + 拦截记录          |
| `configure_fingerprint(f)`                          | 指纹控制             | Camoufox 内置反检测                 |

## 安装

```bash
pip install camoufox
camoufox fetch          # 下载真实 Firefox 反检测构建（约 1.3GB，可配 gh-proxy 镜像加速）
```

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
audit/WHITEBOX_AUDIT.md    官方 release 白盒审计报告（sha256 验证 / PE 分析 / 架构揭示 / 重建路径）
audit/NATIVE_CAPABILITIES.md native 能力审计（三层架构 / RPC op 全集 / Necko 管道协议 / 网络栈源码 / 重建映射）
examples/verify_demo.py    模拟验证系统示例（加载→执行 JS→提取 token→点击）
```

## 许可证与致谢

- 本项目代码：MIT（自研）。
- 引擎：Camoufox（Firefox 内核，MPL-2.0）。
- 设计参考：ruyiJS（MPL-2.0，见 `audit/WHITEBOX_AUDIT.md` 对其 release 的审计）。
