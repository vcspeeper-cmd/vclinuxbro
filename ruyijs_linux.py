#!/usr/bin/env python3
"""ruyijs-linux: ruyiJS 的 Linux 自托管开源实现（Context API 层）

对齐 ruyiJS 设计理念（真实引擎执行网站 JS + 隔离上下文 + 代理隔离），
但完全 Linux 原生 + 纯开源组件：
  - ruyiJS 用 SpiderMonkey 158 + Necko 网络栈（Windows 专有、runtime 闭源）
  - 本实现用 Camoufox（真实 Firefox 内核反检测浏览器，playwright 驱动）作为引擎
  - 保持 ruyiJS 的 Context API 形状，方便理解与迁移

Context 语义（对齐 ruyiJS）:
  - 每个 Context = 一个隔离的浏览器上下文（独立 cookie/存储/代理/指纹）
  - Context 是常驻对象，可反复 run_js / open / fetch，会话不泄漏
  - 可设置代理（每上下文独立）

用法:
  from ruyijs_linux import Context
  ctx = Context(proxy="http://127.0.0.1:7890")
  html = ctx.open("https://example.com/login")      # 打开页面（等 JS 执行）
  token = ctx.run_js("window._token || document.body.innerText")  # 执行任意 JS 取 token
  ok = ctx.click("a[href*='verify']")                # 点击页面内验证元素
  print(ctx.title(), ctx.url())
  ctx.close()

依赖: pip install camoufox && camoufox fetch  （真实 Firefox 反检测构建）
"""
import json, os, re, time, threading, tempfile, urllib.parse

class Context:
    """一个隔离的浏览器上下文（对齐 ruyiJS Context：隔离进程 + 真实引擎 + 独立代理）"""

    def __init__(self, proxy=None, headless=True, user_agent=None, locale="zh-CN",
                 viewport=(1280, 720), timeout=40000, name=None):
        self.proxy = proxy
        self.headless = headless
        self.user_agent = user_agent
        self.locale = locale
        self.viewport = viewport
        self.timeout = timeout
        self.name = name or ("ctx-" + os.urandom(3).hex())
        self._browser = None
        self._page = None
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def _ensure(self):
        if self._browser is not None:
            return
        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            raise RuntimeError("camoufox 未安装：pip install camoufox && camoufox fetch")
        launch_kw = dict(headless=self.headless, humanize=True)
        if self.proxy:
            # Camoufox 支持 proxy 参数（http/socks）
            launch_kw["proxy"] = self.proxy
        self._browser = Camoufox(**launch_kw)
        ctx = self._browser.new_context(
            user_agent=self.user_agent,
            locale=self.locale,
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
        )
        self._page = ctx.new_page()

    def open(self, url, wait_ms=2000, wait_until="domcontentloaded"):
        """打开页面并等 JS 执行（对齐 ruyiJS: 执行网站自己的 JS）"""
        self._ensure()
        with self._lock:
            resp = self._page.goto(url, timeout=self.timeout, wait_until=wait_until)
            if wait_ms:
                self._page.wait_for_timeout(wait_ms)
            return resp.status if resp else 0

    def run_js(self, js, timeout_ms=15000):
        """在页面上下文执行任意 JS，返回结果（对齐 ruyiJS run JS API）。
        支持返回 dict/list/str/int/float/bool；非 JSON 序列化值转 str。"""
        self._ensure()
        with self._lock:
            expr = "JSON.stringify((function(){%s})())" % _wrap_expr(js)
            try:
                raw = self._page.evaluate(expr, timeout=timeout_ms)
            except Exception:
                # 直接 evaluate 表达式
                raw = self._page.evaluate(js, timeout=timeout_ms)
            return _safe_parse(raw)

    def fetch(self, url, method="GET", headers=None, body=None, wait_ms=1000):
        """在浏览器上下文内 fetch（走 Necko 对等的浏览器网络栈，带真实指纹/cookie）"""
        self._ensure()
        with self._lock:
            js = "fetch('%s',%s).then(r=>r.text()).catch(e=>'ERR:'+e.message)" % (
                url, json.dumps(_fetch_init(method, headers, body)))
            return self._page.evaluate(js, timeout=self.timeout)

    def click(self, selector, wait_ms=1500):
        """点击页面内元素（如验证按钮/链接），返回点击后标题"""
        self._ensure()
        with self._lock:
            try:
                self._page.click(selector, timeout=10000)
            except Exception as e:
                return {"ok": False, "error": str(e)[:120]}
            if wait_ms:
                self._page.wait_for_timeout(wait_ms)
            return {"ok": True, "title": self.title(), "url": self.url()}

    def extract(self, selector):
        """提取元素文本/属性"""
        self._ensure()
        with self._lock:
            return self._page.evaluate(
                "(el=>el?{text:el.innerText.slice(0,200),html:el.outerHTML.slice(0,300)}:null)"
                "(document.querySelector('%s'))" % selector)

    # ---- 状态 ----
    def title(self):
        return self._page.title() if self._page else ""

    def url(self):
        return self._page.url if self._page else ""

    def content(self, max_len=3000):
        return self._page.content()[:max_len] if self._page else ""

    def cookies(self):
        if self._page is None:
            return []
        return [c for c in self._page.context.cookies()]

    def set_cookie(self, name, value, domain=None, url=None):
        self._ensure()
        if url:
            self._page.context.add_cookies([{"name": name, "value": value, "url": url}])
        else:
            self._page.context.add_cookies([{"name": name, "value": value, "domain": domain}])

    def screenshot(self, path=None):
        self._ensure()
        path = path or ("/tmp/%s.png" % self.name)
        self._page.screenshot(path=path)
        return path

    def close(self):
        with self._lock:
            if self._browser is not None:
                try:
                    self._browser.stop()
                except Exception:
                    pass
                self._browser = None
                self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---- 工具 ----
def _wrap_expr(js):
    js = js.strip()
    # 若已是表达式（不以 return/语句开头），包成 return
    if not js.startswith(("return ", "var ", "let ", "const ", "if", "for", "while", "function", "{")):
        return "return (" + js + ");"
    return js

def _safe_parse(raw):
    if isinstance(raw, (dict, list, str, int, float, bool)) or raw is None:
        return raw
    return str(raw)

def _fetch_init(method, headers, body):
    init = {"method": method}
    if headers:
        init["headers"] = headers
    if body is not None:
        init["body"] = body
    return init


# ---- 便捷工厂：对齐 ruyiJS 一次调用执行一段 JS 的 quick start ----
def quick(js_or_url, proxy=None, wait_ms=2000):
    """一条命令：打开页面并执行 JS，返回结果。
    quick("https://a.com") -> 页面 HTML 摘要
    quick(("https://a.com", "window._token")) -> 打开后执行 JS 取 token"""
    ctx = Context(proxy=proxy)
    try:
        if isinstance(js_or_url, tuple):
            url, js = js_or_url
            ctx.open(url, wait_ms=wait_ms)
            return ctx.run_js(js)
        if js_or_url.startswith(("http://", "https://")):
            ctx.open(js_or_url, wait_ms=wait_ms)
            return {"title": ctx.title(), "url": ctx.url(), "html": ctx.content(800)}
        return ctx.run_js(js_or_url)
    finally:
        ctx.close()


if __name__ == "__main__":
    import sys
    # CLI: ruyijs_linux.py <url> [js]
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    js = sys.argv[2] if len(sys.argv) > 2 else "document.title"
    with Context() as ctx:
        ctx.open(url)
        print("URL:", ctx.url())
        print("TITLE:", ctx.title())
        print("JS 结果:", ctx.run_js(js))
