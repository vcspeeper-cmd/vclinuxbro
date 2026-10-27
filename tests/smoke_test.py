"""ruyijs-linux 冒烟自检脚本。

验证三件事：
1. 模块可导入（依赖 camoufox/playwright 已装则真实导入；未装则降级为 API 契约检查）
2. Context API 契约完整（ruyiJS 对照表要求的全部方法存在）
3. verify_demo 示例可解析编译

用法：
    python tests/smoke_test.py          # 快速自检（不启动浏览器）
    python tests/smoke_test.py --full   # 完整（需 camoufox fetch 过的环境）
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# ruyiJS 官方绑定完整 API 契约（从 vendor/ruyijs-binding-src 8 个控制面 + _context.py 提取）
# 覆盖：Context 核心 / Environment / Scheduling / Random / Resource / Console / Inspector / Input / Evaluation
REQUIRED_API = [
    # Context 核心（_context.py）
    "load_page", "navigate", "checkpoint", "collect_garbage", "expose", "set",
    "native", "close", "closed",
    # EnvironmentControl（_environment.py）
    "configure_fingerprint", "configure_environment", "get_defaults",
    # SchedulingControl（_scheduling.py）
    "configure_clock", "clock_status", "configure_timer_replay", "pending_timers",
    "timer_realm", "configure_message_replay", "pending_messages", "run_message",
    "run_timer", "configure_navigation_replay", "navigation_replay_status",
    "advance", "drain_timers", "sleep", "tick",
    # RandomControl（_random.py）
    "configure_random", "configure_crypto_replay", "crypto_replay_status",
    # ResourceControl（_resources.py）
    "export_cookies", "cookie_header", "put_resource", "get_resource",
    "resource_status", "net_log", "clear_resources",
    # ConsoleControl（_console.py）
    "console_log", "clear_console",
    # InspectorControl（_inspector.py，debug mode）
    "peek_api_log", "get_api_log", "clear_api_log",
    # InputControl（_input.py）
    "dispatch_input", "click", "type", "press",
    # EvaluationControl（_context_eval.py）
    "eval", "locals",
]
CTOR_KW = ["proxy", "headless", "timeout", "budget", "shell", "environment", "mode"]


def check_import() -> bool:
    try:
        from ruyijs_linux import Context  # noqa: F401
        print("[OK] ruyijs_linux 导入成功（camoufox 已装）")
        return True
    except ImportError as e:
        missing = str(e).split("No module named ")[-1].strip("'\"")
        print(f"[WARN] ruyijs_linux 导入失败: 缺依赖 {missing}（API 契约检查仍执行）")
        return False


def check_api_contract() -> bool:
    spec = importlib.util.spec_from_file_location(
        "ruyijs_linux_check", os.path.join(REPO, "ruyijs_linux.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # 只编译加载，不启动浏览器
    except Exception:
        traceback.print_exc()
        print("[FAIL] ruyijs_linux.py 编译/加载失败")
        return False

    cls = getattr(mod, "Context", None)
    if cls is None:
        print("[FAIL] 无 Context 类")
        return False

    missing = [m for m in REQUIRED_API if not hasattr(cls, m)]
    if missing:
        print(f"[FAIL] 缺少方法: {missing}")
        return False
    print(f"[OK] Context API 契约完整（{len(REQUIRED_API)} 个方法全部存在）")
    return True


def check_example_compile() -> bool:
    ex = os.path.join(REPO, "examples", "verify_demo.py")
    if not os.path.exists(ex):
        print("[WARN] examples/verify_demo.py 不存在")
        return True
    with open(ex) as f:
        compile(f.read(), ex, "exec")
    print("[OK] examples/verify_demo.py 语法编译通过")
    return True


def check_network_channel() -> bool:
    """验证纯 Python 网络替代通道（engine='necko' 自动回退标准库，零依赖）。
    起一个本地 http.server，实测 _stdlib_fetch 与 _necko_fetch 的 GET/POST。"""
    import http.server
    import json
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps({"ok": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            payload = json.dumps({"echo": body.decode("utf-8", "replace")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        from ruyijs_linux import Context
        ctx = Context(timeout=5000)
        r1 = ctx._stdlib_fetch(f"http://127.0.0.1:{port}/ping")
        assert r1["status"] == 200, f"stdlib GET status={r1['status']}"
        assert '"ok"' in r1["text"], f"stdlib GET body={r1['text']!r}"
        r2 = ctx._necko_fetch(f"http://127.0.0.1:{port}/echo", method="POST",
                              headers={"Content-Type": "text/plain"}, body="hi")
        assert r2["status"] == 200, f"necko 回退 POST status={r2['status']}"
        assert '"hi"' in r2["text"], f"necko 回退 POST body={r2['text']!r}"
        print("[OK] 网络替代通道（stdlib + necko 回退）本机实测通过")
        return True
    except Exception:
        traceback.print_exc()
        print("[FAIL] 网络替代通道验证失败")
        return False
    finally:
        srv.shutdown()


def check_full() -> bool:
    """完整模式：真实启动 Context 并跑一段 JS（需 camoufox fetch 过）。"""
    try:
        from ruyijs_linux import Context
    except ImportError:
        print("[SKIP] 完整模式需要 camoufox 依赖")
        return True
    with Context(headless=True) as ctx:
        ctx.open("data:text/html,<script>window.__t='hello'</script>")
        v = ctx.run_js("window.__t")
        assert v == "hello", f"run_js 结果异常: {v!r}"
        print("[OK] 完整模式：Context 启动 + open + run_js 全通")
    return True


def main() -> int:
    full = "--full" in sys.argv[1:]
    ok = True
    ok &= check_import() if full else check_api_contract()
    ok &= check_example_compile()
    ok &= check_network_channel()
    if full:
        ok &= check_full()
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
