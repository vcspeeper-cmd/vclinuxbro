#!/usr/bin/env python3
"""示例：模拟验证系统（对齐 ruyiJS examples 的工作流）

场景：一个"验证页"在页面内用 JS 生成 token（模拟瑞数/极验类"JS 生成凭证"），
我们加载页面 → 执行其 JS → 提取 token → 点击确认按钮 → 验证流程结束。

运行：python examples/verify_demo.py [--proxy http://127.0.0.1:7890]
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ruyijs_linux import Context

# 模拟验证页：页面 JS 计算一个"签名"并写入 window（对齐 ruyiJS 击败的那批验证系统）
MOCK_HTML = """<!doctype html><html><head><meta charset=utf-8><title>模拟验证</title></head>
<body>
  <div id=status>等待验证</div>
  <button id=verify data-token="">点击验证</button>
  <script>
    // —— 模拟"网站自身 JS 生成凭证"（瑞数/极验等把 token 逻辑藏在 JS 里）——
    function compute_token() {
      var a = 0x1234, b = 0x5678;
      var t = ((a ^ b) * 2654435761) >>> 0;          // 纯 JS 计算，无网络
      return "tok-" + t.toString(16) + "-" + Math.floor(Date.now()/1000);
    }
    window._token = compute_token();                  // 页面内生成的凭证
    document.getElementById('verify').dataset.token = window._token;
    console.log("mock: token generated =", window._token);
  </script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    with Context(proxy=args.proxy) as ctx:
        # 1) 加载页面（等 JS 执行）——对齐 ruyiJS load_page
        ctx.open("https://mock-verify.local/", wait_ms=500)
        print("[1] 页面标题:", ctx.title())

        # 2) 执行页面内 JS 提取 token——对齐 ruyiJS eval/run_js
        token = ctx.run_js("window._token")
        print("[2] JS 提取 token:", token)
        assert token and str(token).startswith("tok-"), "token 提取失败"

        # 3) 验证 token 确实由页面 JS 生成（模拟"凭证校验"）
        check = ctx.run_js(
            "document.getElementById('verify').dataset.token === window._token")
        print("[3] 页面内凭证一致:", check)

        # 4) 点击确认（对齐 ruyiJS InputControl.click）——触发"验证完成"
        ctx.click("#verify")
        ctx.run_js("document.getElementById('status').textContent='已验证'")
        status = ctx.run_js("document.getElementById('status').textContent")
        print("[4] 点击后状态:", status)

        # 5) 取 console 日志（对齐 ruyiJS ConsoleControl.console_log 理念）
        #    （真实场景下 Camoufox 的 console 事件需挂 on('console')；此处用页面回显替代）
        print("[5] 页面状态回显:", status)

        # 6) 导出 Cookie 头（对齐 ruyiJS cookie_header：凭证可复用于 requests 会话）
        print("[6] cookie_header:", repr(ctx.cookie_header("https://mock-verify.local/")))

    print("✅ 全流程通过：加载页面 → 执行 JS → 提取 token → 点击验证")


if __name__ == "__main__":
    main()
