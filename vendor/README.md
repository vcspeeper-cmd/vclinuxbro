# vendor/necko-python-src — wheel 内 Python 网络栈源码（白盒提取）

## 来源
- 上游: `martinkeith700/ruyiJS` release `v0.1.5` → `ruyijs-0.1.5-py3-none-win_amd64.whl`
- 提取方式: zipfile 只读（**零执行**），路径前缀 `ruyijs/_runtime/vendor/necko-derived/python/Lib/site-packages/`
- 隔离审计目录: `oasis:/opt/ruyijs-audit/`

## 内容
| 包 | 文件数 | 说明 |
|---|---|---|
| `necko/` | 10 py + 5 数据 | requests 兼容层 + NSS 栈 HTTP/2、HTTP/3 传输实现（`_http2.py`/`_http3.py`/`_nss.py`/`_transport.py`） |
| `ruyijs_necko/` | 66 py | **纯 Python worker 框架**：管道协议、会话/信用/预算/注册表、oneshot 启动入口、webtransport |
| `necko_requests.py` | 1 | 入口：`from necko import requests` 的薄封装（415B） |
| `brotli.py` | 1 | 纯 Python brotli 回退（2027B） |
| `necko/licenses/` | 2 | firefox-license.html / firefox-notice.txt（NSS/Firefox 许可文本） |
| `necko/*.pem/.dat/.json` | 3 | firefox-roots.pem（184KB 根证书）、firefox-suffixes.dat（334KB PSL）、quic-build.json |

## 排除项（Windows-only，未入库）
以下 DLL 为 **NSS 栈二进制**，仅存在于 win_amd64 wheel，已从仓库排除但 hash 记录如下
（存放于 `oasis:/opt/ruyijs-audit/wheel-py/` 供静态审计）：

| DLL | 大小 | sha256 |
|---|---|---|
| nss3.dll | 2.9MB | `9d7fd394f553475502bccdf8b26b8f36c0267f355ec4b6a0bdb2c1b4484cf32b` |
| necko_quic.dll | 2.4MB | `d158838ec2e2fa0be60c3879c146688697f6f0675117c569ba981901ec937a0d` |
| freebl3.dll | 1.1MB | `e2fabaa0f7096f5b3bff95b17456f86156f36e7583a82f667cae2e9a28c63c29` |
| mozglue.dll | 655KB | `3d00724f1ada1076668380fa690c169756991f6b65455c0c8050ad46364d1686` |
| softokn3.dll | 327KB | `b8fc11f61aa9fa9a6723a3248959e4418b26ef153dcd469909283efd8224ddbb` |
| necko_core.dll | 126KB | `7791840ab30294db90ac40f4bf7a68d4435644cdeefe5321f474b0be3b8c4717` |
| ruyijs_brotli.dll | 178KB | `ef4c9ab61b03a0f11ff2c0739fc909a822ff187d16405ffe4b3f9d874b104ac8` |

## Linux 重建含义
- `ruyijs_necko/` **100% 可移植**（纯 Python，仅依赖标准库 + 少量第三方如 brotli/cffi）
- `necko/` 的 HTTP/2、HTTP/3 传输依赖 NSS DLL → Linux 需以
  `aioquic`(HTTP/3) / `h2`(HTTP/2) / 系统 OpenSSL 替代，或直接用 `httpx`/`curl_cffi`
- 上游 wheel 同时内嵌完整 Python 3.14 运行时（`vendor/necko-derived/python/`，未提取——体积大且
  Linux 可用系统 Python 替代；如需可另行白盒提取）
- 数据文件（firefox-roots.pem / firefox-suffixes.dat）跨平台可用，直接复用

## 校验
- 逐文件 sha256 记录见提取日志（oasis 终端），本目录文件与 wheel 内字节级一致
- wheel 全量 sha256（43.4MB 文件本身）见 `audit/WHITEBOX_AUDIT.md`
