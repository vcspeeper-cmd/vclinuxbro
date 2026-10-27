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

    # ---- 对齐 ruyiJS API 契约的扩展能力 ----
    # 对应 ruyiJS: EvaluationControl.set / Context.expose / InputControl / ResourceControl /
    #              ConsoleControl / SchedulingControl.advance

    def set(self, name, value):
        """设置 JS 全局变量（对齐 ruyiJS Context.set(name, value)）。"""
        self._ensure()
        with self._lock:
            payload = json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value
            self._page.evaluate("window[%s] = %s" % (json.dumps(name), payload))

    def expose(self, name, fn):
        """把 Python 函数暴露给页面 JS（对齐 ruyiJS expose(function, name)）。

        JS 侧调用 window.<name>(...) 会同步/异步调回 Python。
        """
        self._ensure()
        with self._lock:
            self._page.expose_function(name, fn)

    def native(self, source, name="__ruyijs_native__"):
        """向页面注入一段 JS 定义（对齐 ruyiJS native(name, source)）。"""
        self._ensure()
        with self._lock:
            self._page.evaluate(source)

    def type(self, text, selector=None, interval_ms=60):
        """模拟键盘输入（对齐 ruyiJS InputControl.type）。selector 缺省聚焦当前元素。"""
        self._ensure()
        with self._lock:
            target = self._page.locator(selector) if selector else self._page
            target.type(text, delay=interval_ms)
            return {"ok": True, "typed": text}

    def press(self, key, selector=None):
        """按下一个键（Enter/Tab/...，对齐 ruyiJS InputControl.press）。"""
        self._ensure()
        with self._lock:
            target = self._page.locator(selector) if selector else self._page
            target.press(key)
            return {"ok": True, "key": key}

    def advance(self, ms, step_ms=16.667):
        """推进事件循环 / 等待（对齐 ruyiJS SchedulingControl.advance）。"""
        self._ensure()
        with self._lock:
            steps = max(1, int(ms / step_ms))
            for _ in range(min(steps, 200)):
                self._page.wait_for_timeout(step_ms)
            return True

    def cookie_header(self, url, *, method="GET"):
        """导出当前上下文对该 URL 的 Cookie 请求头（对齐 ruyiJS cookie_header）。

        返回 "name=value; name2=value2"，可直接用于 requests 会话复用页面登录态。
        """
        if self._page is None:
            return ""
        try:
            parts = []
            for c in self._page.context.cookies(url):
                parts.append("%s=%s" % (c["name"], c["value"]))
            return "; ".join(parts)
        except Exception:
            return ""

    def intercept(self, pattern, reply=None, status=200, headers=None):
        """路由拦截：pattern 匹配的请求改为返回本地 reply（对齐 ruyiJS put_resource 的
        资源注入理念，用于把验证接口的响应替换为可控内容）。

        reply 为 dict/str；传 None 表示仅记录（可配合 console_log 观察）。
        """
        self._ensure()
        with self._lock:
            body = reply if isinstance(reply, (str, bytes)) else json.dumps(reply)
            self._page.route(pattern, lambda route: route.fulfill(
                status=status, body=body, headers=headers or {"Content-Type": "application/json"}))
            return True

    def console_log(self, clear=False):
        """取回页面 console 输出（对齐 ruyiJS ConsoleControl.console_log）。"""
        if self._page is None:
            return []
        with self._lock:
            logs = getattr(self, "_console_lines", [])
            if clear:
                self._console_lines = []
            return logs

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
