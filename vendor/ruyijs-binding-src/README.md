# vendor/ruyijs-binding-src — ruyiJS Python 绑定 + C ABI 客户端源码

## 来源
- 上游: `martinkeith700/ruyiJS`（已 fork `vcspeeper-cmd/ruyiJS`）
- 本地审计克隆: `/data/data/com.termux/files/home/ruyijs-src`（浅克隆，与 fork 远端一致）
- 提取方式: 直接复制，**未修改任何字节**

## 内容（25 文件）

### python-ruyijs/（20 文件）— Python 绑定层
| 文件 | 职责 |
|---|---|
| `__init__.py` | 公开 API：`Context` / `RuntimeNotFound` / 版本 |
| `_context.py` | Context 生命周期 + 子进程托管（named pipe RPC 客户端） |
| `_pipe.py` | named pipe 客户端协议（写请求/读响应） |
| `_context_eval.py` / `_eval_options.py` | eval 选项与 JS 值封装 |
| `_jsvalue.py` / `_values.py` / `_public_values.py` | JS 值 ↔ Python 转换 |
| `_callbacks.py` / `_console.py` / `_inspector.py` | 回调/console/调试器 |
| `_environment.py` / `_scheduling.py` / `_random.py` / `_input.py` / `_resources.py` | 9 大控制面绑定 |
| `_runtime_paths.py` | 定位 wheel 内运行时 |
| `_version.py` | 版本常量 |
| `examples/__init__.py` / `examples/__main__.py` | CLI 示例 |

### src-client/（5 文件）— C ABI named-pipe 客户端
| 文件 | 职责 |
|---|---|
| `ruyijs.h` / `context.h` | 头文件（C ABI 契约） |
| `api.cpp` | 顶层 API 实现 |
| `lifecycle.cpp` | 运行时子进程启动/销毁 |
| `transport.cpp` | named-pipe 传输（写请求/读响应） |

## 校验
- 逐文件 sha256 记录于 2026-10-27 提取日志（与 fork 源码字节级一致）
- 与 wheel 内 `ruyijs/` Python 层比对结论见 `audit/WHITEBOX_AUDIT.md` §源码 vs wheel

## Linux 重建含义
- `python-ruyijs/` 100% 可移植（纯 Python + 标准库）
- `src-client/` 为 C++ named-pipe 客户端，Linux 重建时 `transport.cpp` 需
  `CreateNamedPipe`/`WaitForSingleObject` → unix domain socket / 进程管道适配
  （详见 `audit/LINUX_REBUILD_PATH.md` §3 与 `audit/NATIVE_CAPABILITIES.md` §管道协议）
