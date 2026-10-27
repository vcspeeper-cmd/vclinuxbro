# ruyiJS Native 能力审计报告（src/runtime + src/transport + resource_network）

> 审计日期：2026-10-27 · 审计对象：`vcspeeper-cmd/ruyiJS` fork（`main` @ v0.1.5）
> 审计方式：`git clone --depth 1` 本地静态阅读 + 对 oasis 隔离目录内官方 wheel 做 zipfile 白盒清单比对（全程未运行任何 release 二进制）
> 上游 commit 顶层实际含 **LICENSE（MPL-2.0）** —— GitHub API 的 `NOASSERTION` 是 SPDX 识别误判，**本项目许可为 Mozilla Public License 2.0（弱 copyleft，允许再分发/修改/商业使用，作者礼貌性请求非约束条款）**。

---

## 1. 三层架构全景（修正版）

```
┌─ Layer 1: Python 绑定层  python/ruyijs/（19 文件）
│    Context / JSError / JSObject / JSFunction 公开 API
│    子进程管理（--ruyijs-rpc <pipe>）+ JSON 帧协议 + callback 事件循环 + 16MiB 帧上限
│
├─ Layer 2: C ABI 客户端层  src/client/（5 文件）
│    api.cpp / context.h / lifecycle.cpp / ruyijs.h / transport.cpp
│    Windows named pipe 客户端（OVERLAPPED I/O，ConnectNamedPipe/ReadFile/WriteFile，deadline 控制）
│
├─ Layer 3: Native 引擎层  src/（40 个目录 ≈ 1,700+ 文件）
│    ├─ runtime/        （147 文件）RPC 服务端、9 大控制面、页面生命周期、WebIDL 接口工厂
│    ├─ transport/      （29 文件）Necko 子进程管道协议（Preface/BodyHeader/握手/Job 限制）
│    ├─ resource_network/（234 文件）AsyncHttp 会话池 + Necko Python 网络栈桥 + WebTransport
│    └─ 37 个 web_*/browser_environment/document_model/interaction/domain/…  Web 平台 API 实现
│         （browser_environment 91、document_model 305、interaction 97、web_media 94、
│           web_geometry 73、web_platform 73、web_styling 69、web_canvas 78、…）
```

**关键架构事实**：无窗口、无渲染进程；`js.exe` 静态链接 SpiderMonkey 158（Firefox/158.0 UA 伪装）+ 全部自研 WebIDL 接口；网络请求经 `transport/` 双通道管道派发给内嵌 **CPython 3.14 + requests/necko 包**（即「Necko 会话传输」）。

---

## 2. RPC 协议（runtime/rpc.cpp `Serve()` 全 op 清单）

`Pipe.Read` 读帧 → `ParseRequest`（JSON 对象）→ 按 `op` 分发 → `Reply`（codec.reply(id, success, value)）→ `Pipe.Write`。
请求帧必须含 `id`（number|string）+ `op`（string）。以下 op 会让环境/调度先 `Freeze()`（冻结指纹后再执行）：`eval/set/expose/expose_data/native/page/input/user/locals`。

| op | 处理者 | 语义 |
|---|---|---|
| `eval` | EvalRequest | 页面上下文执行 JS（host 标记） |
| `locals` | hostValues.Encode(global) | 返回全局对象编码 |
| `host_value` | hostValues.Request | 活动 JS 值句柄请求 |
| `set` | bridge.Set(name, value) | 注入单文档全局（跨导航**不**重放） |
| `expose` | bridge.Expose(name) | 暴露宿主函数（Playwright exposeFunction 语义，**跨导航重放**） |
| `expose_data` | bridge.ExposeData(name, value) | 写 `__ruyijs__.data` 条目（跨导航重放） |
| `native` | bridge.NativeWrap(name, code, constructor) | 用 JS 源码字符串包装 native（constructor 可选 bool） |
| `configure` / `defaults` | environment.Request | 环境指纹配置（Navigator/Screen/viewport/timezone…） |
| `configure_random` | ConfigureRandom | 随机种子固定 |
| `configure_crypto_replay` / `crypto_replay_status` | pages | crypto 重放控制（确定性） |
| `event_loop` | scheduling.Request | 事件循环运行/描述 |
| `timer_realm` | scheduling.TimerRealm | 定时器 realm 选择（时钟推进 16.667ms 帧步进，100000 步上限） |
| `resource` | resources.Request | 资源注入（put_resource：离线响应） |
| `cookies` | CookieRequest | cookie 控制（读/写/清除） |
| `console` | console.Request | 读取/清空页面 console（action: log|clear） |
| `api_log` | monitor.Request | debug 模式 API 监控日志 |
| `page` | pages.Request | 页面加载/导航（load_page/navigate） |
| `input` | InputRequest | 输入模拟（type/press/advance） |
| `user` | UserInputRequest | user activation 模拟 |
| `collect` | CollectGarbage | 显式 GC |
| `checkpoint` | checkpoint(cx) | 让调度检查点通过 |
| `close` | — | 关闭连接结束循环 |

### HostBridge（runtime/host_bridge.h）——Python ↔ JS 桥契约
- `Expose(name)`：注册函数回调，`nextCallId_` 递增，经 `Call`/`ReadReply`/`EncodeEvent` 往返
- `Set(name, value)`：单文档全局，不跨导航
- `ExposeData(name, value)`：`__ruyijs__.data` 条目
- `NativeWrap(name, source, constructor)`：把 JS 源码字符串包装成 native 构造器
- `Activate(page, helpers)`：新顶层文档重放 expose/native/expose_data 注册（导航存活）
- `healthy()` 健康标志：pipe 断裂即失效

### WebKit 工厂（runtime/web_kit.cpp）——WebIDL 接口定义机制
- `Define(name, clasp, parent, ctor, face, len)`：构造 WebIDL 接口对象（length/name/prototype/toStringTag）
- `Method` / `Accessor`：原型方法/访问器 + `PageHelpers` 记录 member 轨迹
- JSON 序列化：`JsonValue` 有 **1 MiB session 上限**；帧总上限 16 MiB（`Pipe::kMaxFrame`）

### Runtime 装配（runtime/runtime.cpp `Run()`）
- `JS_SetGCParameter(JSGC_COMPACTING_ENABLED, 0)`：关 GC 压缩（D16 偏差，donor 引擎 relocating GC 故障）
- `PrewarmNeckoSessions(2)`：后台预启动 2 个 Necko 会话，首个请求不等待解释器
- 资源所有者生命周期：BrowsingContext > Frames > TopLevel > Scheduling > Resource（port 绑定守卫全部 RAII）

---

## 3. Necko 管道协议（transport/，Windows named pipe）

### 3.1 通道与命名（necko_channel.cpp）
- 命名：`\\.\pipe\ruyijs-necko-<32hex>`（`BCryptGenRandom` 生成 16 字节随机 → hex）
- 安全：`PIPE_REJECT_REMOTE_CLIENTS` + 双端 PID 校验（`GetNamedPipeClientProcessId` / `GetNamedPipeServerProcessId`）
- **双通道**：`control_` + `body_`（会话级复用）
- 帧限制：64 KB payload 上限（`kBodyPayloadLimit = 65536`）、8 MiB body（请求/响应）

### 3.2 帧格式（worker_frame.h）
```
Preface 40B : role(2 LE) | epoch(8) | pid(4) | nonce(16)
BodyHeader 48B: type(2) | epoch(8) | requestId(8) | generation(4) | hop(4) | seq(8) | payloadLength(4) | flags(4)
```
- 握手 `Prefaces`：control+body 双通道互换 Preface 并校验 role/epoch/pid/nonce
- `Hello`：JSON 消息 `{"role","pid","nonce","manifest","qpcFrequency"}` 双端校验（manifest 为 64 字符 hex）
- 消息：`Send(type, payload)` / `Receive(ControlDocument)`，`ReceiveNext` 30s stall；seq 单调递增

### 3.3 子进程启动（necko_child_start.cpp + necko_job.h）
- `CreateProcessW(DETACHED_PROCESS | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT)`
- 先 `CreateProcessW` → `job.Assign` → `ResumeThread`（挂起状态下先入 Job 再恢复，防逃逸）
- 标准输入/输出重定向到 `NUL`（Handle Allowlist）
- **Job 资源限制**：processes ≤ 3、单进程内存 ≤ 1.5 GiB、Job 内存 ≤ 2 GiB、KILL_ON_JOB_CLOSE

### 3.4 会话池（resource_network/necko_http.cpp）
- `kSessions = 18`、`kPerOrigin = 6`（Firefox `network.http.max-persistent-connections-per-server` 语义）
- Lease 机制：每槽一次事务；helper 消失（未读到 frame）时重试 1 次，保留原 deadline
- 启动命令：`"<python>" -I -B -m ruyijs_necko.oneshot --session --timing-v2`
- 后台线程 `PrewarmNeckoSessions(2)`

---

## 4. Necko Python 网络栈解密（重建最大利好）

### 4.1 源码可得性（wheel site-packages 白盒清单）
| 包 | 条目 | 内容 |
|---|---|---|
| `necko/` | 25 | `__init__.py` `api.py` `adapters.py` `sessions.py` `_connect.py` `_firefox.py` `_http2.py` `_http3.py` `_nss.py` `_socks.py` `_retry.py` `_transport.py` + Firefox 指纹静态资源（`firefox-profile.json` `firefox-roots.pem` `firefox-suffixes.dat` `firefox-root-constraints.json`） |
| `ruyijs_necko/` | 60 | `oneshot.py` `request_control.py` `launch.py` `lease.py` `body_reader.py` `codec.py` `decoder.py` `upload.py` `connection_budget.py` `connection_control.py` `connection_lifetime.py` `command_budget.py` `controlled_send.py` `socket_control.py` `stream_control.py` `resolver.py` `resolver_child.py` `cache_snapshot.py` `coalescing_snapshot.py` `transport_resources.py` `webtransport_{session,verify,wire,worker}.py` `wire_fields.py` `worker_body_{handoff,outbox}.py` 等 |
| 依赖 | — | requests 2.34.2 / urllib3 2.7.0 / h2 4.4.1 / hpack 4.2.0 / hyperframe / brotli / certifi / idna / charset-normalizer / publicsuffixlist / cryptography / cffi / pycparser / zstandard |
| 内嵌解释器 | — | CPython 3.14（`python.exe` + `python314.dll` + `python314.zip` + `DLLs/*.pyd`） |

### 4.2 oneshot 进程协议（src/resource_network/necko_oneshot.py，162 行完整可读）
- 入口：`python -I -B -m ruyijs_necko.oneshot [--session|--webtransport] [--timing-v2]`
- stdin/stdout framing（4 字节小端长度前缀 + UTF-8 字段，字段 ≤ 65536）
- 请求：`method | url | header_count | headers[] | body_len(≤8MiB) | body`
- 响应：`status | header_count | headers[] | payload_len | payload | trailer`
- **trailer**：`0x32545252`(=RRT2, timing-v2) / `0x31545252`(=RRT1) | encoded | decoded | `http/1.0|http/1.1|h2|h3` | `snapshot(control)`（3×double：时间预算快照）
- 参数：`BUDGET=15s`（总预算，含 TCP/代理 CONNECT/TLS，对齐 Firefox 90s 连接超时）、`READ=10s`、`ATTEMPTS=RequestControl.MAX_ATTEMPTS`、`LIMIT=8MiB`（响应/请求 body）
- 代理：环境变量 `RUYIJS_HTTP_PROXY`（http/https），严格校验防 CRLF 注入
- 会话模式 `--session`：同一 adapter 串行服务多请求，按源复用连接

### 4.3 Windows-only 二进制清单（Linux 重建唯一硬缺口）
```
necko/necko_core.dll   — NSS 封装核心（TLS 1.3 / ALPN）
necko/necko_quic.dll   — HTTP/3 QUIC
necko/nss3.dll + freebl3.dll + softokn3.dll + mozglue.dll — Mozilla NSS TLS 栈
ruyijs_necko/ruyijs_brotli.dll — native brotli 编解码
```

---

## 5. 9 大控制面 → C++ 实现映射（与 Python 绑定层交叉验证一致）

| 控制面（Python 模块） | C++ 实现（runtime/） | 关键行为 |
|---|---|---|
| Environment（_environment.py） | `environment_control.h/cpp` | `File(profile)` 读环境 profile、`Request(configure/defaults)`、`Freeze()` |
| Scheduling（_scheduling.py） | `scheduling_control.h` | `Pass(ms)` 时钟推进（16.667ms 帧步进、100000 步上限）、TimerRealm、MessageSelection |
| Random（_random.py） | `random_control.h` | `ConfigureRandom`（种子固定） |
| Resource（_resources.py） | `resource_control.h` + `resource_network/realm.h` | `Request(resource)`：put_resource 离线响应、cookie 联合 |
| Console（_console.py） | `console_control.h` | `{action: log|clear}` 读 Context 级有界日志 |
| Input（_input.py） | `input_control.h` + `user_input.h` | `InputRequest`/`UserInputRequest`（type/press/advance、user activation） |
| Evaluation（_context_eval.py） | `eval_request.cpp` + `host_values` | eval 类型转换、host_value 句柄 |
| Inspector（_inspector.py） | `native_constructor_trace.h` + `exception_capture.h` + `api_monitor.h` | 异常捕获、API 监控（debug 模式）、native 构造器 trace |
| Callbacks（_callbacks.py） | `host_bridge.h` + `host_callback.cpp` | expose 回调往返、nextCallId 协议 |

---

## 6. Linux 自托管重建：组件映射表

| 组件 | 来源 | 可移植性 | 缺口/方案 |
|---|---|---|---|
| SpiderMonkey 158 JS 引擎 | Mozilla 官方源码（MPL-2.0） | ✅ 官方支持 Linux | 编译（clang/gcc，作者 Windows 用 MSVC） |
| src/ 40 目录 Web 平台 API（C++） | 本仓（MPL-2.0） | ⚠️ 需移植 | Windows API → POSIX（文件系统/线程/时钟）；**无窗口** 简化了大部分移植 |
| runtime/（RPC/控制面） | 本仓 | ⚠️ 需移植 | named pipe → unix domain socket（协议帧不变） |
| transport/（管道/Job） | 本仓 | ⚠️ 需移植 | CreateProcessW/Job → fork+setrlimit/waitpid；PID 校验保留 |
| **necko/ + ruyijs_necko/ Python 包** | wheel site-packages（**纯 Python 源码**） | ✅ **直接可用** | 无缺口（requests/h2/h3 均跨平台） |
| **NSS TLS 栈**（nss3/necko_core/necko_quic/freebl3/softokn3/mozglue） | wheel（Windows DLL） | ❌ **唯一硬缺口** | 用 Mozilla 官方 Linux NSS 构建替代，或回退 requests+urllib3 标准 TLS（牺牲部分指纹真实性） |
| ruyijs_brotli.dll | wheel | ❌ | 用 pip 版 brotli 替代 |
| firefox-roots.pem / suffixes.dat / profile.json | wheel（静态资源） | ✅ 直接可用 | 无缺口 |
| Python 绑定层 python/ruyijs | 本仓 | ✅ | 管道层替换为 unix socket |

**结论**：网络栈（necko + ruyijs_necko，85 个纯 Python 文件）与指纹资源**全部可从 wheel 白盒提取并跨平台复用**；真正需要重新编译/移植的是 (a) SpiderMonkey 158（官方 Linux 源码可得）、(b) src/ 40 目录 C++（Windows API → POSIX 移植）、(c) NSS TLS 栈（官方 Linux NSS 替代）。这比最初设想的「重写整个浏览器引擎」工作量小一个数量级。

---

## 7. 附：审计证据链
- fork 仓库顶层：LICENSE=MPL-2.0（17KB）、NOTICE 声明 Mozilla 派生 + CPython 内嵌 + requests 全家桶
- runtime.json：`shell_sha256=30581e7a…`（与 js.exe 实测 sha256 完全一致）
- wheel `ruyijs/_runtime/vendor/necko-derived/`：内嵌 CPython 3.14 + site-packages（necko/ 25 + ruyijs_necko/ 60 + requests 生态）
- src/runtime/rpc.cpp `Serve()` 22 个 RPC op 全集（本节 §2 表）
- transport/worker_frame.h：40B Preface + 48B BodyHeader 帧协议（本节 §3.2 表）
