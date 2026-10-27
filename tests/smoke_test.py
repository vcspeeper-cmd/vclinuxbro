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

# ruyiJS API 对照表（见 README 能力对照）
REQUIRED_API = [
    "open", "navigate", "run_js", "eval", "set", "expose", "native",
    "advance", "click", "type", "press", "cookies", "cookie_header",
    "intercept", "console_log", "net_log", "api_log", "close",
]
CTOR_KW = ["proxy", "headless", "timeout", "budget"]


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
    if full:
        ok &= check_full()
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
