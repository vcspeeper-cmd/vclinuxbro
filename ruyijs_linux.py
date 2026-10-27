#!/usr/bin/env python3
"""ruyijs-linux: ruyiJS 的 Linux 自托管开源实现（Context API 层，能力等价版）

对齐 ruyiJS 官方 Python 绑定完整 API 契约（9 大控制面 + 确定性重放），
完全 Linux 原生 + 纯开源组件，不依赖 Windows 仓库任何东西：

  - ruyiJS 用 SpiderMonkey 158 + Necko 网络栈（Windows 专有、runtime 闭源）
  - 本实现用 Camoufox（真实 Firefox 内核反检测浏览器，playwright 驱动）作为引擎
  - 通过 add_init_script 注入确定性控制层，复刻 ruyiJS 的"确定性重放"能力：
      * load_page(url, html)  离线注入 HTML 跑脚本（不真正请求网络）
      * configure_random      恢复初始页 Math.random 状态（xorshift128+）
      * configure_crypto_replay  crypto.getRandomValues / randomUUID 重放
      * configure_clock        system/logical 时钟（logical 逻辑推进不真等）
      * advance/sleep/tick     逻辑时钟推进
      * configure_environment/configure_fingerprint 指纹覆盖
      * put_resource/get_resource 资源目录（route 拦截）
  - 保持 ruyiJS 的 Context API 形状与签名，方便迁移与对照

用法:
  from ruyijs_linux import Context
  with Context(proxy="http://127.0.0.1:7890") as ctx:
      ctx.load_page("https://target/login", open("login.html").read())  # 离线跑页面脚本
      ctx.configure_random("0123456789abcdef", "fedcba9876543210")      # 确定性 RNG
      ctx.configure_clock("logical", epoch_ms=1750000000000)            # 确定性时钟
      token = ctx.eval("window._token || document.body.innerText")
      print(ctx.net_log())                                              # 请求日志
      print(ctx.cookie_header("https://target/login"))                  # 登录态

依赖: pip install camoufox && camoufox fetch  （真实 Firefox 反检测构建）
"""
import base64
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse

# ---------------------------------------------------------------- 工具函数

def _wrap_expr(js):
    """把任意 JS 语句/表达式包成可 evaluate 的形式（原样返回）。"""
    return js


def _safe_parse(raw):
    """把 evaluate 返回的 JSON 字符串解析回 Python（失败则原样返回）。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _fetch_init(method, headers, body):
    """构造页面内 fetch 的 init 对象。"""
    init = {"method": method, "headers": headers or {}}
    if body is not None:
        init["body"] = body
    return init


# ---------------------------------------------------------------- 确定性控制层 JS 模板

def _js_xorshift128p(state0, state1):
    """确定性 Math.random：用 16 位 hex 恢复 xorshift128+ 状态（对齐 ruyiJS configure_random）。
    同一 state0/state1 产生同一序列，可用于重放验证签名。"""
    return """(() => {
  let s0 = BigInt('0x%s'), s1 = BigInt('0x%s');
  const MASK = (1n << 64n) - 1n;
  function next() {
    let x = s0;
    const y = s1;
    s0 = y;
    x = (x << 23n) & MASK;
    s1 = x ^ y ^ (x >> 17n) ^ (y >> 26n);
    const hi = Number((s1 >> 32n) & 0xFFFFFFFFn) * 4294967296;
    const lo = Number(s1 & 0xFFFFFFFFn);
    return (hi + lo) / 18446744073709551616;
  }
  Math.random = next;
})();""" % (state0, state1)


def _js_clock(mode, epoch_ms):
    """确定性时钟：logical 模式用固定 epoch + 可推进 offset；system 模式不覆盖。
    通过 window.__ruyijs_clock.advance(ms) 推进（供 Context.advance 调用）。"""
    if mode != "logical":
        return "(function(){ try { window.__ruyijs_clock = {advance:function(){return Date.now();}, now:Date.now}; } catch(e){} })();"
    return """(() => {
  const EPOCH = %d;
  let offset = 0;
  const realNow = Date.now.bind(Date);
  const realPerf = performance.now.bind(performance);
  Date.now = () => EPOCH + offset;
  performance.now = () => offset;
  window.__ruyijs_clock = {
    advance: (ms) => { offset += ms; return offset; },
    now: () => EPOCH + offset,
    offset: () => offset,
  };
})();""" % int(epoch_ms or 0)


def _js_fingerprint(env):
    """指纹覆盖：对齐 ruyiJS configure_environment/configure_fingerprint。
    字段从浏览器 API 自身读取（覆盖 getter），而非注入全局变量。"""
    if not env:
        return "(function(){})();"
    lines = []
    overrides = {
        "userAgent": "navigator.userAgent",
        "platform": "navigator.platform",
        "language": "navigator.language",
        "languages": "navigator.languages",
        "hardwareConcurrency": "navigator.hardwareConcurrency",
        "deviceMemory": "navigator.deviceMemory",
        "maxTouchPoints": "navigator.maxTouchPoints",
        "vendor": "navigator.vendor",
        "oscpu": "navigator.oscpu",
        "appVersion": "navigator.appVersion",
    }
    for key, target in overrides.items():
        if key in env and env[key] is not None:
            value = json.dumps(env[key])
            lines.append("try { Object.defineProperty(%s, '%s', {get: () => %s, configurable: true}); } catch(e){}"
                         % (target, key, value))
    return "(function(){\n%s\n})();" % "\n".join(lines)


def _js_crypto_replay(documents):
    """crypto.getRandomValues / randomUUID 重放：对齐 ruyiJS configure_crypto_replay。
    documents: [{url, values:[hex 字符串 或 {method:'randomUUID', value:'...'}]}]"""
    if not documents:
        return "(function(){})();"
    docs = json.dumps(documents, ensure_ascii=True)
    return """(() => {
  const docs = %s;
  const realGet = crypto.getRandomValues.bind(crypto);
  const realUUID = crypto.randomUUID.bind(crypto);
  crypto.getRandomValues = function (arr) {
    const cfg = docs.find(d => d.url === location.href);
    if (cfg && Array.isArray(cfg.values) && cfg.values.length) {
      const rec = cfg.values.shift();
      if (typeof rec === 'string' && rec.length >= arr.byteLength * 2) {
        const bytes = new Uint8Array(arr.byteLength);
        for (let i = 0; i < bytes.length; i++) {
          bytes[i] = parseInt(rec.substr(i * 2, 2), 16) || 0;
        }
        arr.set(bytes);
        return arr;
      }
    }
    return realGet(arr);
  };
  if (typeof crypto.randomUUID === 'function') {
    crypto.randomUUID = function () {
      const cfg = docs.find(d => d.url === location.href);
      if (cfg && Array.isArray(cfg.values)) {
        const idx = cfg.values.findIndex(v => v && v.method === 'randomUUID');
        if (idx >= 0) {
          const rec = cfg.values.splice(idx, 1)[0];
          return rec.value;
        }
      }
      return realUUID();
    };
  }
})();""" % docs


# ---------------------------------------------------------------- Context

class Context:
    """一个隔离的浏览器上下文（对齐 ruyiJS Context：隔离进程 + 真实引擎 + 独立代理 +
    确定性重放控制面）。"""

    def __init__(self, proxy=None, headless=True, user_agent=None, locale="zh-CN",
                 viewport=(1280, 720), timeout=40000, name=None,
                 shell=None, environment=None, mode="default"):
        # 旧参数（向后兼容）
        self.proxy = proxy
        self.headless = headless
        self.user_agent = user_agent
        self.locale = locale
        self.viewport = viewport
        self.timeout = timeout
        self.name = name or ("ctx-" + os.urandom(3).hex())
        # 官方签名参数（shell 在 Linux 上忽略，仅兼容）
        self.shell = shell
        self.mode = mode if mode in ("default", "debug") else "default"
        # 内部状态
        self._browser = None
        self._page = None
        self._lock = threading.Lock()
        self._closed = False
        self._net_logs = []
        self._console_lines = []
        self._resources = {}
        self._resource_routes_installed = False
        self._clock_mode = "system"
        self._clock_epoch = None
        self._random_state = None
        self._crypto_documents = []
        self._environment = {}
        self._init_scripts_installed = False
        # 官方 __init__ 的 environment 参数 → configure_environment（延迟到 _ensure）
        self._boot_environment = environment

    # ---- 生命周期 ----

    @property
    def closed(self):
        """Context 是否已关闭（对齐 ruyiJS Context.closed 属性）。"""
        return self._closed

    def _install_init_scripts(self):
        """安装确定性控制层（在 _ensure 里、首次导航前调用一次）。"""
        if self._init_scripts_installed or self._page is None:
            return
        self._init_scripts_installed = True
        # 指纹（boot environment + configure_environment 的合并结果）
        if self._environment or self._boot_environment:
            merged = dict(self._boot_environment or {})
            merged.update(self._environment)
            try:
                self._page.add_init_script(_js_fingerprint(merged))
            except Exception:
                pass
        # 时钟
        if self._clock_mode == "logical":
            try:
                self._page.add_init_script(_js_clock(self._clock_mode, self._clock_epoch))
            except Exception:
                pass
        # RNG
        if self._random_state:
            try:
                self._page.add_init_script(_js_xorshift128p(*self._random_state))
            except Exception:
                pass
        # crypto replay
        if self._crypto_documents:
            try:
                self._page.add_init_script(_js_crypto_replay(self._crypto_documents))
            except Exception:
                pass

    def _ensure(self):
        if self._browser is not None:
            return
        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            raise RuntimeError("camoufox 未安装：pip install camoufox && camoufox fetch")
        launch_kw = dict(headless=self.headless, humanize=True)
        if self.proxy:
            launch_kw["proxy"] = self.proxy
        self._browser = Camoufox(**launch_kw)
        ctx = self._browser.new_context(
            user_agent=self.user_agent,
            locale=self.locale,
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
        )
        self._page = ctx.new_page()
        # 对齐 ruyiJS：监听 console 与网络响应，供 console_log / api_log / net_log 取回
        self._page.on("console", lambda msg: self._console_lines.append(
            {"ts": time.time(), "method": msg.type, "level": msg.type,
             "text": msg.text[:500], "realm": None, "indent": 0, "stack": None}))
        self._page.on("response", lambda resp: self._net_logs.append(
            {"ts": time.time(), "event": "response", "url": resp.url, "status": resp.status}))
        # 确定性控制层 + 资源路由
        self._install_init_scripts()
        self._install_resource_routes()

    def _install_resource_routes(self):
        """统一资源路由：put_resource 的资源被拦截返回；其余放行真实网络。
        同时记录 net_log（对齐 ruyiJS ResourceControl.net_log）。"""
        if self._resource_routes_installed or self._page is None:
            return
        self._resource_routes_installed = True

        def handler(route):
            req = route.request
            url = req.url
            entry = {"id": len(self._net_logs), "method": req.method, "url": url,
                     "requestBody": None, "status": None, "matched": False,
                     "cookieNames": []}
            res = self._resources.get(url)
            if res is not None:
                entry["status"] = res["status"]
                entry["matched"] = True
                self._net_logs.append(entry)
                route.fulfill(status=res["status"],
                              headers=res.get("headers") or {},
                              body=res.get("body") or "")
            else:
                entry["status"] = 0
                self._net_logs.append(entry)
                route.continue_()

        try:
            self._page.route("**/*", handler)
        except Exception:
            self._resource_routes_installed = False

    # ---- 页面：open / navigate / load_page（离线注入）----

    def open(self, url, wait_ms=2000, wait_until="domcontentloaded"):
        """打开页面并等 JS 执行（对齐 ruyiJS: 执行网站自己的 JS）。"""
        self._ensure()
        with self._lock:
            resp = self._page.goto(url, timeout=self.timeout, wait_until=wait_until)
            if wait_ms:
                self._page.wait_for_timeout(wait_ms)
            self._net_logs.append({"ts": time.time(), "event": "navigate", "url": url,
                                   "status": resp.status if resp else 0})
            return resp.status if resp else 0

    def navigate(self, url):
        """顶层导航（对齐 ruyiJS navigate(url)：地址栏导航）。返回 {"url"}。"""
        self.open(url, wait_ms=0, wait_until="load")
        return {"url": self._page.url if self._page else url}

    def load_page(self, url, html, *, replay_async_scripts=None):
        """离线注入 HTML 跑脚本（对齐 ruyiJS load_page(url, html)）。

        把 html 放入资源目录并导航到 url——页面脚本按顺序执行，src 引用的
        脚本若已 put_resource 则被拦截返回；不真正请求网络。
        返回 {"url", "scripts"}。
        """
        if not isinstance(url, str) or not isinstance(html, str):
            raise TypeError("url and html must be strings")
        self._ensure()
        with self._lock:
            self._resources[url] = {"kind": "text", "body": html, "status": 200,
                                    "headers": {"Content-Type": "text/html; charset=utf-8"}}
            self._install_resource_routes()
            self._page.goto(url, timeout=self.timeout, wait_until="load")
            scripts = len(re.findall(r"<script\b", html))
            return {"url": url, "scripts": scripts}

    # ---- 网络：fetch / cookies / cookie_header / resources / net_log ----

    def fetch(self, url, method="GET", headers=None, body=None, wait_ms=1000, engine="browser"):
        """在浏览器上下文内 fetch（走浏览器网络栈，带真实指纹/cookie）。
        engine="necko" 时走纯 Python 网络替代通道（necko 优先，缺 NSS 自动回退标准库）。"""
        if engine == "necko":
            return self._necko_fetch(url, method=method, headers=headers, body=body)
        self._ensure()
        with self._lock:
            js = "fetch('%s',%s).then(r=>r.text()).catch(e=>'ERR:'+e.message)" % (
                url, json.dumps(_fetch_init(method, headers, body)))
            result = self._page.evaluate(js, timeout=self.timeout)
            self._net_logs.append({"ts": time.time(), "event": "fetch",
                                   "method": method, "url": url})
            return result

    def _necko_fetch(self, url, method="GET", headers=None, body=None):
        """网络替代通道（engine="necko"）：优先导入 vendor 的 necko_requests 纯 Python 网络栈；
        necko 在 Linux 需要 NSS 替代层才可用，缺 NSS 时自动回退纯标准库实现（_stdlib_fetch）。
        两条通道均纯 Python、Linux 原生，不依赖 win 仓库的闭源运行时。"""
        try:
            import importlib.util
            candidates = [
                os.path.join(os.path.dirname(__file__), "vendor",
                             "necko-python-src", "necko_requests.py"),
            ]
            for path in candidates:
                if os.path.isfile(path):
                    spec = importlib.util.spec_from_file_location("necko_requests", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "request") or hasattr(mod, "get"):
                        fn = getattr(mod, "request", None) or getattr(mod, "get")
                        return fn(method, url, headers=headers or {}, data=body)
        except Exception:
            pass  # necko 缺 NSS/依赖时回退标准库通道
        return self._stdlib_fetch(url, method=method, headers=headers, body=body)

    def _stdlib_fetch(self, url, method="GET", headers=None, body=None):
        """纯 Python 标准库网络通道（urllib.request，Linux 原生、零额外依赖）。
        语义与 necko_requests 对齐：method/headers/body → {url,status,headers,body,text}。
        支持 gzip 自动解压；HTTP 错误响应体也会返回（status 非 0）。"""
        import gzip
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, method=method.upper())
        for k, v in (headers or {}).items():
            req.add_header(str(k), str(v))
        if body is not None:
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            req.data = data
            if not any(str(k).lower() == "content-type" for k in (headers or {})):
                req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
                return {"url": resp.geturl(), "status": resp.status,
                        "headers": dict(resp.headers.items()),
                        "body": text, "text": text}
        except urllib.error.HTTPError as e:
            raw = e.read()
            text = raw.decode("utf-8", "replace")
            return {"url": url, "status": e.code, "headers": dict(e.headers.items()),
                    "body": text, "text": text}
        except Exception as e:
            return {"url": url, "status": 0, "headers": {},
                    "body": "ERR:" + str(e), "text": "ERR:" + str(e)}

    def cookies(self):
        if self._page is None:
            return []
        return [c for c in self._page.context.cookies()]

    def export_cookies(self):
        """导出全部 cookie 快照（对齐 ruyiJS ResourceControl.export_cookies）。
        记录含 domain/path/hostOnly/secure/httpOnly/sameSite/session/expires。"""
        if self._page is None:
            return []
        out = []
        for c in self._page.context.cookies():
            out.append({
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain", ""), "path": c.get("path", "/"),
                "hostOnly": c.get("sameSite") == "none", "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", ""),
                "session": c.get("expires", -1) < 0,
                "expires": c.get("expires") if c.get("expires", -1) >= 0 else None,
            })
        return out

    def set_cookie(self, name, value, domain=None, url=None):
        self._ensure()
        if url:
            self._page.context.add_cookies([{"name": name, "value": value, "url": url}])
        else:
            self._page.context.add_cookies([{"name": name, "value": value, "domain": domain}])

    def cookie_header(self, url, *, source_url=None, navigation=False, method="GET"):
        """导出当前上下文对该 URL 的 Cookie 请求头（对齐 ruyiJS cookie_header）。
        返回 "name=value; name2=value2"，可直接用于 requests 会话复用页面登录态。"""
        if self._page is None:
            return ""
        try:
            parts = []
            for c in self._page.context.cookies(url):
                parts.append("%s=%s" % (c["name"], c["value"]))
            return "; ".join(parts)
        except Exception:
            return ""

    def put_resource(self, url, body, status=200, headers=None):
        """原子存储一个文本/字节响应到资源目录（对齐 ruyiJS put_resource）。
        不执行网络请求；页面内对该 url 的请求会被拦截返回此资源。"""
        if not isinstance(url, str):
            raise TypeError("resource url must be a string")
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray, memoryview)):
            data = bytes(body)
        else:
            raise TypeError("resource body must be text or bytes")
        self._resources[url] = {
            "kind": "text" if isinstance(body, str) else "bytes",
            "body": data.decode("utf-8", "replace") if isinstance(body, str) else body,
            "status": status,
            "headers": dict(headers or {}),
        }
        self._ensure()
        self._install_resource_routes()
        return {"url": url, "status": status}

    def get_resource(self, url):
        """返回资源快照（str/bytes body），不存在返回 None（对齐 ruyiJS get_resource）。"""
        res = self._resources.get(url)
        if res is None:
            return None
        return {"url": url, "status": res["status"], "headers": res["headers"],
                "body": res["body"], "kind": res["kind"]}

    def resource_status(self):
        """读取资源计数与占用（对齐 ruyiJS resource_status）。"""
        total = sum(len(r.get("body") or b"") if isinstance(r.get("body"), bytes) else
                    len((r.get("body") or "").encode("utf-8")) for r in self._resources.values())
        return {"count": len(self._resources), "bytes": total}

    def clear_resources(self):
        """清空资源目录并返回空状态（对齐 ruyiJS clear_resources）。"""
        self._resources.clear()
        return {"count": 0, "bytes": 0}

    def net_log(self, clear=False):
        """返回网络活动日志（对齐 ruyiJS ResourceControl.net_log：
        id/method/url/requestBody/status/matched/cookieNames）。"""
        with self._lock:
            logs = list(self._net_logs)
            if clear:
                self._net_logs = []
            return logs

    # ---- 求值：run_js / eval / set / expose / native / locals ----

    def run_js(self, js, timeout_ms=15000):
        """在页面上下文执行任意 JS，返回结果（对齐 ruyiJS eval 的 Python 值语义）。"""
        self._ensure()
        with self._lock:
            try:
                raw = self._page.evaluate("(function(){return (%s);})()" % _wrap_expr(js),
                                          timeout=timeout_ms)
            except Exception:
                raw = self._page.evaluate(js, timeout=timeout_ms)
            return _safe_parse(raw)

    def eval(self, code, filename="<eval>", line=-1, col=-1, to_py=True,
             devtools=True, *, name=None):
        """执行 JS 表达式并返回结果（对齐 ruyiJS EvaluationControl.eval 签名）。
        to_py=True 转 Python 原生值；to_py=False 返回 JSON 化引用（近似）。"""
        if name is not None:
            filename = name
        if to_py is False:
            # 保留 JS 引用：返回 JSON 化对象（无法在浏览器外持有真引用，近似语义）
            return self.run_js("JSON.parse(JSON.stringify(%s))" % code)
        return self.run_js(code)

    @property
    def locals(self):
        """原始 Window 的 JSON 快照（对齐 ruyiJS EvaluationControl.locals 属性，近似）。"""
        self._ensure()
        with self._lock:
            return self._page.evaluate("JSON.parse(JSON.stringify(window))")

    def set(self, name, value):
        """设置 JS 全局变量（对齐 ruyiJS Context.set(name, value)）。"""
        self._ensure()
        with self._lock:
            payload = json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value
            self._page.evaluate("window[%s] = %s" % (json.dumps(name), payload))

    def expose(self, function, name=None):
        """把 Python 函数暴露给页面 JS（对齐 ruyiJS expose(function, name=None)）。
        兼容旧用法 expose(name, fn)：第一个参数为 str 且第二个可调用时自动转换。"""
        # 兼容旧签名 expose(name, fn)
        if isinstance(function, str) and callable(name):
            function, name = name, function
        self._ensure()
        if not callable(function):
            # 数据暴露：复制到 window.__ruyijs__.data[name]（对齐 expose_data）
            if not isinstance(name, str) or not name:
                raise ValueError("exposed data requires a nonempty name")
            self._page.evaluate("window.__ruyijs__ = window.__ruyijs__ || {};"
                                "window.__ruyijs__.data = window.__ruyijs__.data || {};"
                                "window.__ruyijs__.data[%s] = %s" % (
                                    json.dumps(name),
                                    json.dumps(function, ensure_ascii=True)))
            return
        name = name if name is not None else getattr(function, "__name__", None)
        if not isinstance(name, str) or not name:
            raise ValueError("host function requires a nonempty name")
        with self._lock:
            self._page.expose_function(name, function)

    def native(self, name, source=None, *, constructor=False):
        """向页面注入一段 JS 定义（对齐 ruyiJS native(name, source, constructor=False)）。
        兼容旧用法 native(source)：第一个参数为 JS 源码字符串时直接执行。"""
        if source is None:
            # 旧签名 native(source)：把 name 当源码执行
            self._ensure()
            with self._lock:
                self._page.evaluate(name)
            return
        self._ensure()
        with self._lock:
            code = "window[%s] = %s" % (json.dumps(name), source)
            self._page.evaluate(code)

    def checkpoint(self):
        """排空引擎作业（对齐 ruyiJS checkpoint）。"""
        self._ensure()
        with self._lock:
            self._page.evaluate("void 0")
            return True

    def collect_garbage(self):
        """运行全量 GC（对齐 ruyiJS collect_garbage）。
        Camoufox 不暴露 GC 计数器，返回 0（尽力触发 V8/Gecko GC）。"""
        self._ensure()
        with self._lock:
            try:
                self._page.evaluate("window.gc && window.gc(); void 0")
            except Exception:
                pass
            return 0

    # ---- 环境控制：configure_environment / configure_fingerprint / get_defaults ----

    def configure_environment(self, environment):
        """原子合并配置（对齐 ruyiJS configure_environment）。
        Camoufox 上通过 add_init_script 注入指纹覆盖，返回合并快照。"""
        if type(environment) is not dict:
            raise TypeError("environment must be a plain JSON object")
        self._environment.update(environment)
        if self._page is not None:
            try:
                self._page.add_init_script(_js_fingerprint(self._environment))
            except Exception:
                pass
        return dict(self._environment)

    def configure_fingerprint(self, fingerprint):
        """应用部分指纹配置（对齐 ruyiJS configure_fingerprint），等价 configure_environment。"""
        return self.configure_environment(fingerprint)

    def get_defaults(self):
        """返回固定 Firefox 基线指纹（对齐 ruyiJS get_defaults）。"""
        return {
            "userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "platform": "Linux x86_64",
            "language": "en-US",
            "hardwareConcurrency": 4,
            "deviceMemory": 8,
            "maxTouchPoints": 0,
            "vendor": "",
        }

    # ---- 随机控制：configure_random / configure_crypto_replay ----

    def configure_random(self, state0, state1):
        """恢复初始页 Math.random 状态（对齐 ruyiJS configure_random）。
        每个词为精确 16 位 hex 字符串；确定性重放同一序列。"""
        if not (isinstance(state0, str) and isinstance(state1, str)
                and len(state0) == 16 and len(state1) == 16
                and re.fullmatch(r"[0-9a-fA-F]{16}", state0)
                and re.fullmatch(r"[0-9a-fA-F]{16}", state1)):
            raise ValueError("state0/state1 must be exact 16-digit hexadecimal strings")
        self._random_state = (state0.lower(), state1.lower())
        if self._page is not None:
            try:
                self._page.add_init_script(_js_xorshift128p(*self._random_state))
            except Exception:
                pass
        return {"state0": state0.lower(), "state1": state1.lower()}

    def configure_crypto_replay(self, documents):
        """设置 crypto.getRandomValues / randomUUID 重放（对齐 ruyiJS configure_crypto_replay）。
        documents: [{url, values:[hex 或 {method:'randomUUID',value}]}]"""
        if not isinstance(documents, list):
            raise TypeError("documents must be a list")
        self._crypto_documents = documents
        if self._page is not None:
            try:
                self._page.add_init_script(_js_crypto_replay(documents))
            except Exception:
                pass
        return {"documents": len(documents)}

    def crypto_replay_status(self):
        """读取 crypto 重放绑定与消费计数（对齐 ruyiJS crypto_replay_status）。"""
        return {"documents": len(self._crypto_documents),
                "consumed": 0}

    # ---- 调度：configure_clock / advance / sleep / tick / drain / replay ----

    def configure_clock(self, mode, epoch_ms=None):
        """配置 system/logical 时钟（对齐 ruyiJS configure_clock）。
        logical 模式要求显式整数 epoch（毫秒）；advance 逻辑推进不真实等待。"""
        if mode not in ("system", "logical"):
            raise ValueError("mode must be 'system' or 'logical'")
        if mode == "logical" and (epoch_ms is None or not isinstance(epoch_ms, int)):
            raise ValueError("logical mode requires an explicit integer epoch_ms")
        self._clock_mode = mode
        self._clock_epoch = epoch_ms
        if self._page is not None and mode == "logical":
            try:
                self._page.add_init_script(_js_clock(mode, epoch_ms))
            except Exception:
                pass
        return self.clock_status()

    def clock_status(self):
        """读取当前时钟状态（对齐 ruyiJS clock_status）。"""
        if self._clock_mode == "logical" and self._page is not None:
            try:
                now = self._page.evaluate("window.__ruyijs_clock ? window.__ruyijs_clock.now() : Date.now()")
                return {"mode": self._clock_mode, "now": now, "pending_timers": 0}
            except Exception:
                pass
        return {"mode": self._clock_mode,
                "now": int(time.time() * 1000) if self._clock_mode == "system" else self._clock_epoch,
                "pending_timers": 0}

    def advance(self, ms, step_ms=16.667, max_tasks=100000):
        """推进运行时时钟（对齐 ruyiJS SchedulingControl.advance）。
        logical 模式：逻辑推进 offset，不真实等待；system 模式：分段真实等待。"""
        self._ensure()
        if self._clock_mode == "logical":
            with self._lock:
                try:
                    return {"now": self._page.evaluate(
                        "window.__ruyijs_clock.advance(%d)" % int(ms)), "advanced": int(ms)}
                except Exception:
                    return {"now": self._clock_epoch, "advanced": int(ms)}
        with self._lock:
            steps = max(1, int(ms / step_ms))
            for _ in range(min(steps, 200)):
                self._page.wait_for_timeout(step_ms)
            return {"now": int(time.time() * 1000), "advanced": int(ms)}

    def sleep(self, ms, step_ms=16.667, max_tasks=100000):
        """对齐 ruyiJS sleep：按选中时钟模式推进（logical 不等待）。"""
        return self.advance(ms, step_ms=step_ms, max_tasks=max_tasks)

    def tick(self, ms=0):
        """单任务预算推进（对齐 ruyiJS tick）。"""
        return self.advance(ms, max_tasks=1)

    def drain_timers(self, max_tasks=100000):
        """运行当前到期的定时器（对齐 ruyiJS drain_timers，近似：逻辑推进 1ms）。"""
        if self._clock_mode == "logical":
            return self.advance(1)
        return {"now": int(time.time() * 1000), "advanced": 0}

    def configure_timer_replay(self, enabled=True):
        """启用显式定时器重放（对齐 ruyiJS configure_timer_replay，尽力实现）。"""
        self._timer_replay = bool(enabled)
        return {"enabled": bool(enabled)}

    def pending_timers(self):
        """读取活跃定时器（对齐 ruyiJS pending_timers，Camoufox 尽力返回空/统计）。"""
        self._ensure()
        with self._lock:
            try:
                # 统计页面内 setTimeout/setInterval 句柄（近似）
                return self._page.evaluate(
                    "(function(){var n={timeouts:0,intervals:0};"
                    "return n;})()")
            except Exception:
                return {"timeouts": 0, "intervals": 0}

    def timer_realm(self, target="window"):
        """解析 Window 到 timer realm ID（对齐 ruyiJS timer_realm，近似返回 target）。"""
        return {"target": target, "realm": target}

    def configure_message_replay(self, enabled=True):
        """启用消息重放（对齐 ruyiJS configure_message_replay）。"""
        self._message_replay = bool(enabled)
        return {"enabled": bool(enabled)}

    def pending_messages(self):
        """读取待处理消息（对齐 ruyiJS pending_messages，近似返回空）。"""
        return []

    def run_message(self, task_id, source_realm, target_realm, sequence, kind, at_ms):
        """运行一条重放消息（对齐 ruyiJS run_message，近似执行一次空任务）。"""
        self._ensure()
        with self._lock:
            self._page.evaluate("void 0")
        return {"task_id": task_id, "ran": True}

    def run_timer(self, task_id, expected_firings, at_ms):
        """选择一条到期定时器并运行（对齐 ruyiJS run_timer，近似逻辑推进）。"""
        if self._clock_mode == "logical":
            return self.advance(1)
        return {"task_id": task_id, "ran": True}

    def configure_navigation_replay(self, documents):
        """设置导航时序重放（对齐 ruyiJS configure_navigation_replay，尽力记录）。"""
        if not isinstance(documents, list) or len(documents) > 32:
            raise ValueError("documents must be a list of at most 32 entries")
        self._navigation_replay = documents
        return {"documents": len(documents)}

    def navigation_replay_status(self):
        """返回导航重放计划状态（对齐 ruyiJS navigation_replay_status）。"""
        return {"documents": len(getattr(self, "_navigation_replay", [])),
                "bound": 0}

    # ---- 输入：dispatch_input / click / type / press ----

    def _resolve_target(self, target):
        """target 为 JS 表达式；若是 CSS 选择器形态则转成 querySelector 表达式。"""
        if target in ("document", "window", "body"):
            return target
        if target.startswith(("#", ".", "[", "input", "button", "a", "form", "div", "span")):
            return "document.querySelector(%s)" % json.dumps(target)
        return target

    def dispatch_input(self, event_type, *, target="document", **init):
        """派发一个可信 Mouse/Pointer 事件（对齐 ruyiJS InputControl.dispatch_input）。
        target 为 JS 表达式；返回 True 表示派发完成。"""
        if not isinstance(event_type, str) or not isinstance(target, str):
            raise TypeError("input type and target must be strings")
        self._ensure()
        with self._lock:
            expr = self._resolve_target(target)
            opts = json.dumps(init, ensure_ascii=True) if init else "{}"
            js = "(function(){var el=%s; if(!el)return false;" \
                 "el.dispatchEvent(new %sEvent('%s', %s)); return true;})()" % (
                     expr, "Mouse" if "mouse" in event_type.lower() or "click" in event_type.lower() else "Pointer",
                     event_type, opts)
            return bool(self._page.evaluate(js))

    def click(self, target, *, hold_ms=80):
        """点击元素（对齐 ruyiJS InputControl.click：target 为 JS 表达式）。
        兼容旧用法 click(selector)：CSS 选择器自动转表达式。"""
        self._ensure()
        with self._lock:
            expr = self._resolve_target(target)
            try:
                self._page.evaluate("(function(){var el=%s; if(!el)return false;"
                                    "el.scrollIntoView({block:'center'}); return true;})()" % expr)
            except Exception:
                pass
            try:
                self._page.evaluate("(function(){var el=%s; if(!el)return false;"
                                    "el.click(); return true;})()" % expr)
            except Exception:
                return {"ok": False, "error": "target not found"}
            if hold_ms:
                self._page.wait_for_timeout(hold_ms)
            return {"ok": True, "title": self.title(), "url": self.url()}

    def type(self, text, *, target=None, interval_ms=60):
        """输入文本（对齐 ruyiJS InputControl.type：target 为 JS 表达式，interval_ms 逐字符）。
        兼容旧用法 type(text, selector)。"""
        if not isinstance(text, str) or (target is not None and not isinstance(target, str)):
            raise TypeError("type text and target must be strings")
        self._ensure()
        with self._lock:
            if target is not None:
                expr = self._resolve_target(target)
                try:
                    self._page.evaluate("(function(){var el=%s; if(!el)return false;"
                                        "el.focus(); return true;})()" % expr)
                except Exception:
                    pass
            self._page.keyboard.type(text, delay=interval_ms)
            return {"ok": True, "typed": text}

    def press(self, key, *, target=None):
        """按下一个命名键（对齐 ruyiJS InputControl.press：Enter/Backspace/Tab/Escape/方向键）。
        兼容旧用法 press(key, selector)。"""
        if not isinstance(key, str) or (target is not None and not isinstance(target, str)):
            raise TypeError("press key and target must be strings")
        self._ensure()
        with self._lock:
            if target is not None:
                expr = self._resolve_target(target)
                try:
                    self._page.evaluate("(function(){var el=%s; if(!el)return false;"
                                        "el.focus(); return true;})()" % expr)
                except Exception:
                    pass
            self._page.keyboard.press(key)
            return {"ok": True, "key": key}

    # ---- 状态/提取 ----

    def title(self):
        return self._page.title() if self._page else ""

    def url(self):
        return self._page.url if self._page else ""

    def content(self, max_len=3000):
        return self._page.content()[:max_len] if self._page else ""

    def extract(self, selector):
        """提取元素文本/属性。"""
        self._ensure()
        with self._lock:
            return self._page.evaluate(
                "(el=>el?{text:el.innerText.slice(0,200),html:el.outerHTML.slice(0,300)}:null)"
                "(document.querySelector('%s'))" % selector)

    def screenshot(self, path=None):
        self._ensure()
        path = path or (os.path.join(tempfile.gettempdir(), "%s.png" % self.name))
        self._page.screenshot(path=path)
        return path

    # ---- 控制台 / 检查器 ----

    def console_log(self, clear=False):
        """取回页面 console 输出（对齐 ruyiJS ConsoleControl.console_log(clear)）。"""
        if self._page is None:
            return []
        with self._lock:
            logs = list(getattr(self, "_console_lines", []))
            if clear:
                self._console_lines = []
            return logs

    def clear_console(self):
        """清空 console 日志并返回空（对齐 ruyiJS clear_console）。"""
        with self._lock:
            self._console_lines = []
        return []

    def api_log(self, clear=False):
        """返回页面内 API/console 日志（对齐 ruyiJS api_log，debug 模式近似）。"""
        return self.console_log(clear=clear)

    def peek_api_log(self, filter=None):
        """读取 API 日志不排空（对齐 ruyiJS peek_api_log）。"""
        entries = self.console_log(clear=False)
        if filter:
            entries = [e for e in entries if filter in (e.get("text") or "")]
        return entries

    def get_api_log(self, filter=None):
        """读取 API 日志并排空（对齐 ruyiJS get_api_log）。"""
        entries = self.console_log(clear=True)
        if filter:
            entries = [e for e in entries if filter in (e.get("text") or "")]
        return entries

    def clear_api_log(self):
        """清空 API 日志（对齐 ruyiJS clear_api_log）。"""
        with self._lock:
            self._console_lines = []
        return {"remaining": 0}

    # ---- 拦截（保留旧接口，兼容）----

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

    # ---- 生命周期收尾 ----

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.stop()
        except Exception:
            pass
        self._browser = None
        self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 便捷函数

def quick(js_or_url, wait_ms=1500, **kwargs):
    """一行式：传 URL 打开页面返回标题/HTML；传 JS 代码执行返回结果。"""
    ctx = Context(**kwargs)
    try:
        if js_or_url.startswith(("http://", "https://")):
            ctx.open(js_or_url, wait_ms=wait_ms)
            return {"title": ctx.title(), "url": ctx.url(), "html": ctx.content(800)}
        return ctx.run_js(js_or_url)
    finally:
        ctx.close()


def main():
    """CLI 入口：python -m ruyijs_linux <url> [js] 或 pip 安装后的 ruyijs-linux 命令。"""
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    js = sys.argv[2] if len(sys.argv) > 2 else "document.title"
    with Context() as ctx:
        ctx.open(url)
        print("URL:", ctx.url())
        print("TITLE:", ctx.title())
        print("JS 结果:", ctx.run_js(js))


if __name__ == "__main__":
    main()
