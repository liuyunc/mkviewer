import io
import json
import os
import re
import tempfile
from collections import OrderedDict
from functools import lru_cache
from datetime import timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import gradio as gr
from markdown import Markdown
from minio import Minio
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError
from fastapi.responses import JSONResponse

try:
    from elastic_transport import Transport
except Exception:  # pragma: no cover - optional dependency guard
    Transport = None

try:
    import mammoth
except Exception:  # pragma: no cover - optional dependency guard
    mammoth = None

try:
    import textract
    try:
        from textract.exceptions import ShellError as TextractShellError  # type: ignore
    except Exception:  # pragma: no cover - optional dependency guard
        TextractShellError = Exception
except Exception:  # pragma: no cover - optional dependency guard
    textract = None
    TextractShellError = Exception

# ==================== 环境变量 ====================
MINIO_ENDPOINTS = os.getenv("MINIO_ENDPOINTS", "10.20.41.24:9005,10.20.40.101:9005").split(",")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").strip().lower() == "true"
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
DOC_BUCKET = os.getenv("DOC_BUCKET", "bucket")
DOC_PREFIX = os.getenv("DOC_PREFIX", "")
IMAGE_PUBLIC_BASE = os.getenv("IMAGE_PUBLIC_BASE", "http://10.20.41.24:9005")
SITE_TITLE = os.getenv("SITE_TITLE", "通号院文档知识库")
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "7861"))
MATHJAX_JS_URL = os.getenv(
    "MATHJAX_JS_URL",
    "http://10.20.41.24:9005/cdn/mathjax@3/es5/tex-mml-chtml.js",
)
ENABLE_GRADIO_QUEUE = os.getenv("ENABLE_GRADIO_QUEUE", "false").strip().lower() == "true"
ES_HOSTS = [h.strip() for h in os.getenv("ES_HOSTS", "http://localhost:9200").split(",") if h.strip()]
ES_INDEX = os.getenv("ES_INDEX", "mkviewer-docs")
ES_USERNAME = os.getenv("ES_USERNAME", "")
ES_PASSWORD = os.getenv("ES_PASSWORD", "")
ES_VERIFY_CERTS = os.getenv("ES_VERIFY_CERTS", "true").strip().lower() == "true"
ES_TIMEOUT = int(os.getenv("ES_TIMEOUT", "10"))
ES_COMPAT_VERSION = os.getenv("ES_COMPAT_VERSION", "8").strip()
if ES_COMPAT_VERSION not in {"7", "8"}:  # Elasticsearch 7.x only accepts compat 7 or 8 headers
    ES_COMPAT_VERSION = "8"
ES_MAX_ANALYZED_OFFSET = int(os.getenv("ES_MAX_ANALYZED_OFFSET", "999999"))
if ES_MAX_ANALYZED_OFFSET <= 0:
    ES_MAX_ANALYZED_OFFSET = 999999

ES_ENABLED = bool(ES_HOSTS)

# Inject MathJax with a focused bootstrap that waits for the library to finish
# loading before typesetting, observes preview updates, and surfaces a clear
# error banner when the locally hosted script is unavailable.
_MATHJAX_HEAD_TEMPLATE = """
<script>
(function () {
    var SCRIPT_ID = 'mkv-mathjax-script';
    var PREVIEW_ID = 'doc-html-view';
    var SRC = '__MATHJAX_SRC__';
    var observer = null;
    var observedHost = null;
    var ready = false;
    var pending = false;
    var needsTypeset = true;

    function showFailure(err) {
        console.error('[mkviewer] MathJax 脚本加载失败：' + SRC, err);
        var container = document.getElementById(PREVIEW_ID);
        if (!container) {
            return;
        }
        var banner = container.querySelector('.mathjax-error-banner');
        if (!banner) {
            banner = document.createElement('div');
            banner.className = 'mathjax-error-banner';
            banner.textContent = 'MathJax 脚本加载失败，请检查 MATHJAX_JS_URL 设置或镜像文件。';
            container.insertBefore(banner, container.firstChild || null);
        }
    }

    function getHost() {
        var host = document.getElementById(PREVIEW_ID);
        if (host && typeof host.isConnected === 'boolean' && !host.isConnected) {
            return null;
        }
        return host;
    }

    function typeset(target) {
        var host = target || getHost();
        if (!host) {
            needsTypeset = true;
            requestAnimationFrame(ensureHost);
            return;
        }
        if (observedHost !== host) {
            attachObserver(host);
        }
        if (!ready || !(window.MathJax && window.MathJax.typesetPromise)) {
            needsTypeset = true;
            return;
        }
        needsTypeset = false;
        if (pending) {
            return;
        }
        pending = true;
        requestAnimationFrame(function () {
            pending = false;
            window.MathJax.typesetPromise([host]).catch(function (err) {
                console.error('[mkviewer] MathJax 渲染失败', err);
            });
        });
    }

    function attachObserver(host) {
        if (!host || !window.MutationObserver) {
            return;
        }
        if (observer) {
            observer.disconnect();
        }
        observer = new MutationObserver(function (mutations) {
            for (var i = 0; i < mutations.length; i++) {
                if (mutations[i].type === 'childList') {
                    typeset(host);
                    break;
                }
            }
        });
        observer.observe(host, {childList: true});
        observedHost = host;
    }

    function ensureHost() {
        var host = getHost();
        if (!host) {
            requestAnimationFrame(ensureHost);
            return;
        }
        attachObserver(host);
        if (needsTypeset) {
            typeset(host);
        }
    }

    function configure(win) {
        if (!win) {
            return;
        }
        var cfg = win.MathJax = win.MathJax || {};
        var tex = cfg.tex = cfg.tex || {};
        tex.inlineMath = tex.inlineMath || [['$', '$'], ['\\(', '\\)']];
        tex.displayMath = tex.displayMath || [['$$', '$$'], ['\\[', '\\]']];
        tex.processEscapes = true;
        tex.processEnvironments = true;
        cfg.svg = cfg.svg || {fontCache: 'global'};
        var options = cfg.options = cfg.options || {};
        if (!options.ignoreHtmlClass) {
            options.ignoreHtmlClass = 'tex2jax_ignore';
        }
        var processClass = options.processHtmlClass || '';
        var requiredClasses = ['doc-preview-inner', 'docx-preview', 'arithmatex'];
        if (processClass) {
            var seen = processClass.split('|');
            for (var i = 0; i < requiredClasses.length; i++) {
                if (seen.indexOf(requiredClasses[i]) === -1) {
                    seen.push(requiredClasses[i]);
                }
            }
            processClass = seen.join('|');
        } else {
            processClass = requiredClasses.join('|');
        }
        options.processHtmlClass = processClass;
        if (!options.skipHtmlTags) {
            options.skipHtmlTags = ['script', 'noscript', 'style', 'textarea', 'pre', 'code'];
        }
        var startup = cfg.startup = cfg.startup || {};
        startup.typeset = false;
        var originalReady = typeof startup.ready === 'function' ? startup.ready : null;
        startup.ready = function () {
            if (this && this.startup && typeof this.startup.defaultReady === 'function') {
                this.startup.defaultReady();
            }
            ready = true;
            typeset();
            if (originalReady) {
                try {
                    originalReady.apply(this, arguments);
                } catch (err) {
                    console.error('[mkviewer] MathJax 自定义启动回调失败', err);
                }
            }
        };
    }

    function loadScript(doc) {
        var existing = doc.getElementById(SCRIPT_ID);
        if (existing) {
            if (existing.getAttribute('data-mkv-loaded') === '1' && window.MathJax && window.MathJax.typesetPromise) {
                if (window.MathJax.startup && window.MathJax.startup.promise) {
                    window.MathJax.startup.promise.then(function () {
                        ready = true;
                        typeset();
                    });
                } else {
                    ready = true;
                    typeset();
                }
            }
            ensureHost();
            return;
        }
        var head = doc.head || doc.getElementsByTagName('head')[0] || doc.documentElement;
        if (!head) {
            showFailure(new Error('无法找到 <head> 元素以加载 MathJax'));
            return;
        }
        var script = doc.createElement('script');
        script.id = SCRIPT_ID;
        script.src = SRC;
        script.async = true;
        script.addEventListener('error', function (err) {
            showFailure(err);
        });
        script.addEventListener('load', function () {
            script.setAttribute('data-mkv-loaded', '1');
            if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise) {
                window.MathJax.startup.promise.then(function () {
                    ready = true;
                    typeset();
                }).catch(function (err) {
                    showFailure(err);
                });
            } else if (window.MathJax) {
                ready = true;
                typeset();
            }
        });
        head.appendChild(script);
    }

    function init() {
        configure(window);
        ensureHost();
        loadScript(document);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
</script>
"""

MATHJAX_HEAD = _MATHJAX_HEAD_TEMPLATE.replace("__MATHJAX_SRC__", MATHJAX_JS_URL)

LAYOUT_FALLBACK_HEAD = """
<style>
/* Minimal layout helpers that load with the page head to avoid flash-of-column
   issues when the main stylesheet is still downloading. */
body {
    margin: 0;
    font-family: "PingFang SC","Microsoft YaHei","Source Han Sans SC","Helvetica Neue",Arial,sans-serif;
}
.gradio-container {
    max-width: 1880px;
    width: 100%;
    margin: 0 auto;
    padding: 16px 16px 40px;
}
.gradio-container .gr-row {
    display: flex;
    flex-direction: row !important;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 20px;
}
#layout-main {
    flex-wrap: nowrap;
    align-items: flex-start;
}
#layout-main > .sidebar-col {
    flex: 0 0 300px;
    max-width: 360px;
}
#layout-main > .content-col {
    flex: 1 1 auto;
    min-width: 0;
}
@media (max-width: 768px) {
    .gradio-container {
        padding: 10px 14px 32px;
    }
    .gradio-container .gr-row {
        flex-direction: column !important;
    }
    #layout-main {
        flex-wrap: wrap;
    }
    #layout-main > .sidebar-col,
    #layout-main > .content-col {
        max-width: none;
        flex: 1 1 auto;
    }
}
</style>
"""

# ==================== MinIO 连接 ====================
_client = None
_active_ep = None

def connect() -> Tuple[Minio, str]:
    global _client, _active_ep
    if _client is not None:
        return _client, _active_ep
    last = None
    for ep in [e.strip() for e in MINIO_ENDPOINTS if e.strip()]:
        try:
            c = Minio(ep, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
            c.list_buckets()
            _client, _active_ep = c, ep
            return c, ep
        except Exception as e:
            last = e
    raise RuntimeError(f"无法连接 MinIO：{MINIO_ENDPOINTS} 最后错误：{last}")

# ==================== Elasticsearch 连接 ====================
_es_client: Optional[Elasticsearch] = None


@lru_cache(maxsize=2)
def _compat_transport_class(compat_header: str):
    """Return a Transport subclass that enforces compatibility headers."""
    if Transport is None:  # pragma: no cover - optional dependency guard
        return None

    from inspect import signature, Parameter

    base_sig = signature(Transport.perform_request)
    base_params = base_sig.parameters
    accepts = set(base_params)
    has_var_kw = any(p.kind == Parameter.VAR_KEYWORD for p in base_params.values())

    param_key = None
    for candidate in ("params", "query_params", "query"):
        if candidate in accepts:
            param_key = candidate
            break

    body_key = None
    for candidate in ("body", "request_body"):
        if candidate in accepts:
            body_key = candidate
            break

    class _CompatTransport(Transport):
        def perform_request(self, method, path, params=None, headers=None, body=None, **kwargs):
            hdrs = dict(headers or {})
            # Always overwrite the negotiated compatibility headers because the
            # client populates them with its native major version ("=9") by
            # default, which Elasticsearch 7.x rejects.  Relying on
            # ``setdefault`` or only filling missing keys leaves the
            # incompatible version in place.
            hdrs["accept"] = compat_header
            hdrs["content-type"] = compat_header

            call_kwargs = dict(kwargs)
            request_path = path

            if params is not None:
                if param_key:
                    call_kwargs[param_key] = params
                elif has_var_kw:
                    call_kwargs["params"] = params
                elif params:  # fall back to encoding params into the path
                    query = urlencode(params, doseq=True)
                    sep = "&" if "?" in request_path else "?"
                    request_path = f"{request_path}{sep}{query}"

            if "headers" in accepts or has_var_kw:
                merged = dict(call_kwargs.get("headers", {}))
                merged.update(hdrs)
                call_kwargs["headers"] = merged
            elif hdrs and hdrs != call_kwargs.get("headers"):
                # If the transport truly lacks a headers argument we cannot
                # attach them dynamically, so raise a clear error.
                raise TypeError("Underlying transport does not accept 'headers'")

            if body is not None:
                if body_key:
                    call_kwargs[body_key] = body
                elif has_var_kw:
                    call_kwargs["body"] = body
                else:
                    raise TypeError("Underlying transport does not accept request bodies")

            return super().perform_request(method, request_path, **call_kwargs)

    return _CompatTransport


def es_connect() -> Elasticsearch:
    if not ES_ENABLED:
        raise RuntimeError("未配置 Elasticsearch 主机")
    global _es_client
    if _es_client is not None:
        return _es_client
    kwargs = {
        "hosts": ES_HOSTS,
        "verify_certs": ES_VERIFY_CERTS,
        "request_timeout": ES_TIMEOUT,
    }
    # The Elasticsearch server rejects requests whose Accept/Content-Type advertise
    # a future major version (e.g. "compatible-with=9") with HTTP 400.  Explicitly
    # pinning the compatibility headers avoids the "media_type_header_exception"
    # that surfaced when refreshing the tree view.
    compat_header = f"application/vnd.elasticsearch+json; compatible-with={ES_COMPAT_VERSION}"
    # Some helper APIs overwrite per-request headers, so wrap the transport to
    # guarantee that every call carries the compatibility header instead of the
    # client default (which advertised version 9 and triggered HTTP 400).
    compat_transport = _compat_transport_class(compat_header)
    if compat_transport is not None:
        kwargs["transport_class"] = compat_transport
    kwargs["headers"] = {
        "accept": compat_header,
        "content-type": compat_header,
    }
    if ES_USERNAME or ES_PASSWORD:
        kwargs["basic_auth"] = (ES_USERNAME, ES_PASSWORD)
    _es_client = Elasticsearch(**kwargs)
    ensure_es_index(_es_client)
    return _es_client


def ensure_es_index(es: Elasticsearch) -> None:
    if es.indices.exists(index=ES_INDEX):
        return
    es.indices.create(
        index=ES_INDEX,
        mappings={
            "properties": {
                "path": {"type": "keyword"},
                "title": {"type": "keyword"},
                "content": {"type": "text"},
                "etag": {"type": "keyword"},
                "ext": {"type": "keyword"},
            }
        },
    )


def _es_search_request(
    es: Elasticsearch,
    body: Dict,
    params: Optional[Dict] = None,
    *,
    index: Optional[str] = None,
):
    """Execute a search request while preserving compatibility across client versions."""

    search_index = index or ES_INDEX
    search_params = params or {}
    search_kwargs = {"index": search_index, "body": body}

    if not search_params:
        return es.search(**search_kwargs)

    attempt_errors: List[str] = []
    last_type_error: Optional[TypeError] = None

    def _record_type_error(label: str, exc: TypeError) -> None:
        nonlocal last_type_error
        attempt_errors.append(f"{label}: {exc}")
        last_type_error = exc

    def _try_call(label: str, func):
        try:
            return func()
        except TypeError as exc:
            _record_type_error(label, exc)
            return None

    direct_result = _try_call(
        "es.search(**search_params)",
        lambda: es.search(**search_kwargs, **search_params),
    )
    if direct_result is not None:
        return direct_result

    params_result = _try_call(
        "es.search(params=…)",
        lambda: es.search(**search_kwargs, params=search_params),
    )
    if params_result is not None:
        return params_result

    query_params_result = _try_call(
        "es.search(query_params=…)",
        lambda: es.search(**search_kwargs, query_params=search_params),
    )
    if query_params_result is not None:
        return query_params_result

    options = getattr(es, "options", None)
    option_clients: List[Tuple[str, Elasticsearch]] = []
    if callable(options):
        option_with_params = _try_call(
            "es.options(params=…)",
            lambda: options(params=search_params),
        )
        if option_with_params is not None:
            option_clients.append(("es.options(params=…)", option_with_params))

        option_with_query_params = _try_call(
            "es.options(query_params=…)",
            lambda: options(query_params=search_params),
        )
        if option_with_query_params is not None:
            option_clients.append(("es.options(query_params=…)", option_with_query_params))

        option_default = _try_call("es.options()", lambda: options())
        if option_default is not None:
            option_clients.append(("es.options()", option_default))

    for label, client in option_clients:
        no_param_result = _try_call(
            f"{label}.search()",
            lambda c=client: c.search(**search_kwargs),
        )
        if no_param_result is not None:
            return no_param_result

        direct_option_result = _try_call(
            f"{label}.search(**search_params)",
            lambda c=client: c.search(**search_kwargs, **search_params),
        )
        if direct_option_result is not None:
            return direct_option_result

        option_params_result = _try_call(
            f"{label}.search(params=…)",
            lambda c=client: c.search(**search_kwargs, params=search_params),
        )
        if option_params_result is not None:
            return option_params_result

        option_query_params_result = _try_call(
            f"{label}.search(query_params=…)",
            lambda c=client: c.search(**search_kwargs, query_params=search_params),
        )
        if option_query_params_result is not None:
            return option_query_params_result

    transport = getattr(es, "transport", None)
    if transport is not None:
        def _transport_call():
            path_builder = getattr(es, "_make_path", None)
            path = None
            if callable(path_builder):
                try:
                    path = path_builder(search_index, "_search")
                except Exception:
                    path = None
            if not path:
                idx = search_index
                if isinstance(idx, (list, tuple)):
                    idx = ",".join(idx)
                path = "/_search" if not idx else f"/{idx}/_search"
            return transport.perform_request(
                "POST",
                path,
                params=search_params,
                body=body,
            )

        transport_result = _try_call("transport.perform_request", _transport_call)
        if transport_result is not None:
            return transport_result

    if last_type_error is not None:
        raise TypeError(
            f"{last_type_error} (search attempts: {'; '.join(attempt_errors)})"
        ) from last_type_error

    return es.search(**search_kwargs)

# ==================== 图片链接重写 ====================
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
# 支持的文档类型
SUPPORTED_EXTS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
    ".doc": "doc",
}
MARKDOWN_EXTENSIONS = [
    "fenced_code",
    "tables",
    "codehilite",
    "toc",
    "pymdownx.arithmatex",
]
MARKDOWN_EXTENSION_CONFIGS = {
    "toc": {"permalink": False},
    "pymdownx.arithmatex": {
        "generic": True,
        "tex_inline_wrap": [r"\(", r"\)"],
        "tex_block_wrap": [r"\[", r"\]"],
    },
}
#IMG_EXTS 是一个包含常见图片文件扩展名的元组。它用于快速检查一个文件路径是否以这些扩展名结尾，以确定其是否为图片文件。


def _render_markdown_toc(tokens: List[Dict[str, object]]) -> str:
    """Render a nested table of contents structure from Markdown toc_tokens."""

    def _build(items: List[Dict[str, object]]) -> str:
        parts: List[str] = []
        for item in items:
            name = str(item.get("name") or "").strip()
            anchor = str(item.get("id") or "").strip()
            if not name or not anchor:
                continue
            parts.append("<li>")
            parts.append(f"<a href='#{_esc(anchor)}'>{_esc(name)}</a>")
            children = item.get("children") or []
            if isinstance(children, list):
                child_html = _build(children)
                if child_html:
                    parts.append(child_html)
            parts.append("</li>")
        if not parts:
            return ""
        return "<ul class='toc-list'>" + "".join(parts) + "</ul>"

    tree_html = _build(tokens)
    if not tree_html:
        return ""
    return "<div class='toc-tree'>" + tree_html + "</div>"
def _to_public_image_url(path: str) -> str:
    p = path.strip().lstrip("./").lstrip("/")
    parts = [quote(seg) for seg in p.split("/")]
    return IMAGE_PUBLIC_BASE.rstrip("/") + "/" + "/".join(parts)  

#.rstrip("/"): 移除 IMAGE_PUBLIC_BASE 末尾的 /，以避免出现双斜杠。
#path.strip(): 移除路径字符串开头和结尾的空白字符。
#.lstrip("./"): 移除字符串开头的 ./ 序列（如果存在）。
#.lstrip("/"): 移除字符串开头的 / 字符（如果存在）。
def rewrite_image_links(md_text: str) -> str:
    def repl_md(m):
        alt, url = m.group(1), m.group(2).strip()
        if re.match(r"^https?://", url):
            return m.group(0)
        lower = url.lower()
        if lower.endswith(IMG_EXTS) or any(lower.startswith(p) for p in ("images/","./images/","../images/")):
            return f"![{alt}]({_to_public_image_url(url)})"
        return m.group(0)

    md_text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_md, md_text)

    def repl_img(m):
        url = m.group(1).strip()
        if re.match(r"^https?://", url):
            return m.group(0)
        return m.group(0).replace(m.group(1), _to_public_image_url(url))

    md_text = re.sub(r"<img[^>]+src=\"([^\"]+)\"", repl_img, md_text, flags=re.IGNORECASE)
    md_text = re.sub(r"<img[^>]+src='([^']+)'", repl_img, md_text, flags=re.IGNORECASE)
    return md_text

# ==================== 文档转换辅助 ====================

def _docx_from_bytes(data: bytes) -> Tuple[str, str]:
    if mammoth is None:
        raise RuntimeError("未安装 mammoth，无法预览 DOCX 文档。")
    try:
        html_result = mammoth.convert_to_html(io.BytesIO(data))
        text_result = mammoth.extract_raw_text(io.BytesIO(data))
    except Exception as exc:  # pragma: no cover - 依赖第三方解析
        raise RuntimeError(f"DOCX 解析失败：{exc}") from exc
    text = text_result.value
    html = "<div class='docx-preview'>" + html_result.value + "</div>"
    return text, html

# ==================== 缓存（按 ETag） ====================
class LRU:
    def __init__(self, capacity: int = 512):
        self.cap = capacity
        self.od: OrderedDict[str, tuple] = OrderedDict()
    def get(self, k):
        if k in self.od:
            self.od.move_to_end(k)
            return self.od[k]
        return None
    def set(self, k, v):
        self.od[k] = v
        self.od.move_to_end(k)
        if len(self.od) > self.cap:
            self.od.popitem(last=False)
    def clear(self):
        self.od.clear()

DOC_CACHE = LRU(512)  # key -> (etag, doc_type, text, html, toc)

TREE_DOCS: List[Dict[str, str]] = []

# ==================== 列表/读取 ====================

def list_documents() -> List[Dict[str, str]]:
    c, _ = connect()
    objs = c.list_objects(DOC_BUCKET, prefix=DOC_PREFIX or None, recursive=True)
    docs: List[Dict[str, str]] = []
    for o in objs:
        name = o.object_name
        ext = os.path.splitext(name)[1].lower()
        doc_type = SUPPORTED_EXTS.get(ext)
        if not doc_type:
            continue
        etag = getattr(o, "etag", None) or getattr(o, "_etag", None) or ""
        docs.append({"key": name, "etag": etag, "ext": ext, "doc_type": doc_type})
    docs.sort(key=lambda x: x["key"].lower())
    return docs


def _plain_text_html(text: str) -> str:
    if not text.strip():
        return "<div class='doc-preview-inner doc-preview-empty'><em>文档为空</em></div>"
    esc = _esc(text)
    return "<div class='doc-preview-inner'>" + esc.replace("\n", "<br>") + "</div>"


def get_document(key: str, known_etag: Optional[str] = None) -> Tuple[str, str, str, str, str]:
    """返回 (etag, doc_type, text, html, toc)。"""
    c, _ = connect()
    ext = os.path.splitext(key)[1].lower()
    doc_type = SUPPORTED_EXTS.get(ext)
    if not doc_type:
        raise RuntimeError(f"不支持的文件类型：{ext}")
    if known_etag is None:
        stat = c.stat_object(DOC_BUCKET, key)
        etag = getattr(stat, "etag", None) or getattr(stat, "_etag", None) or ""
    else:
        etag = known_etag
    cached = DOC_CACHE.get(key)
    if cached and cached[0] == etag:
        return cached
    resp = c.get_object(DOC_BUCKET, key)
    data = resp.read()
    resp.close(); resp.release_conn()

    toc_html = ""
    if doc_type == "markdown":
        text = data.decode("utf-8", errors="ignore")
        text2 = rewrite_image_links(text)
        md_renderer = Markdown(
            extensions=MARKDOWN_EXTENSIONS,
            extension_configs=MARKDOWN_EXTENSION_CONFIGS,
        )
        rendered = md_renderer.convert(text2)
        toc_html = _render_markdown_toc(getattr(md_renderer, "toc_tokens", []))
        html = "<div class='doc-preview-inner markdown-body'>" + rendered + "</div>"
    elif doc_type == "docx":
        text, html = _docx_from_bytes(data)
    elif doc_type == "doc":
        converted = False
        if data.startswith(b"PK"):
            try:
                text, html = _docx_from_bytes(data)
                converted = True
            except Exception:
                # 如果伪装成 DOCX 的 DOC 解析失败，继续尝试传统流程
                pass
        if not converted:
            if textract is None:
                raise RuntimeError("未安装 textract 或其依赖，无法预览 DOC 文档。")
            try:
                with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
                    tmp.write(data)
                    tmp.flush()
                    tmp_name = tmp.name
                try:
                    text_bytes = textract.process(tmp_name)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
            except TextractShellError as exc:  # pragma: no cover - 依赖外部命令
                fallback = _decode_possible_text(data)
                if fallback is None:
                    fallback = f"无法解析为有效的 Word 文档：{exc}"
                text = fallback
                html = _plain_text_html(text)
            except Exception as exc:  # pragma: no cover - 其它未知错误
                raise RuntimeError(f"DOC 解析失败：{exc}") from exc
            else:
                text = text_bytes.decode("utf-8", errors="ignore")
                html = _plain_text_html(text)
    else:  # pragma: no cover - 理论上不会走到
        raise RuntimeError(f"未知文档类型：{doc_type}")

    DOC_CACHE.set(key, (etag, doc_type, text, html, toc_html))
    return etag, doc_type, text, html, toc_html

# ==================== 目录树 ====================

def build_tree(files: List[str], base_prefix: str = "") -> Dict:
    tree: Dict = {}
    for key in files:
        rel = key[len(base_prefix):] if base_prefix and key.startswith(base_prefix) else key
        parts = [p for p in rel.split("/") if p]
        cur = tree
        for i, p in enumerate(parts):
            if i == len(parts) - 1:
                cur.setdefault("__files__", []).append(key)
            else:
                cur = cur.setdefault(p, {})
    return tree


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _decode_possible_text(data: bytes) -> Optional[str]:
    """Attempt to coerce binary bytes into readable text for malformed DOC files."""
    if not data:
        return None
    sample = data.strip(b"\x00")
    if not sample:
        return None
    encodings = ("utf-8", "gbk", "gb2312", "latin-1")
    for enc in encodings:
        try:
            text = sample.decode(enc)
        except UnicodeDecodeError:
            continue
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        preview = normalized[:2000]
        total = len(preview)
        if not total:
            continue
        printable = sum(1 for ch in preview if ch.isprintable() or ch in "\n\t")
        if printable / total < 0.6:
            continue
        cleaned = normalized.strip()
        if cleaned:
            return cleaned
    return None


def _file_icon(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in (".doc", ".docx"):
        return "📄"
    return "📝"


def render_tree_html(tree: Dict, expand_all: bool = True) -> str:
    html: List[str] = []
    open_attr = " open" if expand_all else ""
    def rec(node: Dict):
        dirs = sorted([k for k in node.keys() if k != "__files__"], key=str.lower)
        for d in dirs:
            html.append(f"<details{open_attr}><summary>📁 {_esc(d)}</summary>")
            rec(node[d])
            html.append("</details>")
        for key in sorted(node.get("__files__", []), key=str.lower):
            name = key.split("/")[-1]
            link = "?" + urlencode({"key": key})
            html.append(f"<div class='file'>{_file_icon(name)} <a href='{link}'>{_esc(name)}</a></div>")
    rec(tree)
    return "".join(html) if html else "<em>没有找到可预览的文档</em>"


def sync_elasticsearch(docs: List[Dict[str, str]], force: bool = False) -> str:
    if not ES_ENABLED:
        return "<em>未启用 Elasticsearch，跳过索引同步</em>"
    if not docs:
        return "<em>索引同步完成：无可用文档</em>"
    try:
        es = es_connect()
    except Exception as exc:  # pragma: no cover - 运行时依赖外部服务
        return f"<em>索引同步失败：{_esc(str(exc))}</em>"

    try:
        existing_resp = _es_search_request(
            es,
            {
                "size": 10000,
                "query": {"match_all": {}},
                "_source": ["etag"],
            },
        )
        existing_map = {hit["_id"]: hit["_source"].get("etag", "") for hit in existing_resp.get("hits", {}).get("hits", [])}
    except NotFoundError:
        existing_map = {}
    except Exception as exc:  # pragma: no cover - 运行时依赖外部服务
        return f"<em>读取索引失败：{_esc(str(exc))}</em>"

    doc_keys = {d["key"] for d in docs}
    removed = 0
    for stale_id in [k for k in existing_map.keys() if k not in doc_keys]:
        try:
            es.options(ignore_status=[404]).delete(index=ES_INDEX, id=stale_id)
            removed += 1
        except Exception:
            pass

    updated = 0
    errors: List[str] = []
    for doc in docs:
        key = doc["key"]
        etag_hint = doc.get("etag")
        if not force and existing_map.get(key) == etag_hint:
            continue
        try:
            etag, doc_type, text, _, _ = get_document(key, known_etag=etag_hint)
        except Exception as exc:
            errors.append(f"{key}: {exc}")
            continue
        if not text.strip():
            continue
        body = {
            "path": key,
            "title": key.split("/")[-1],
            "content": text,
            "etag": etag,
            "ext": doc_type,
        }
        try:
            es.index(index=ES_INDEX, id=key, document=body)
            updated += 1
        except Exception as exc:  # pragma: no cover - 运行时依赖外部服务
            errors.append(f"{key}: {exc}")
    if updated or removed:
        try:
            es.indices.refresh(index=ES_INDEX)
        except Exception:
            pass
    msg = f"索引同步完成：更新 {updated} 项，移除 {removed} 项"
    if errors:
        escaped = ", ".join(_esc(e) for e in errors[:5])
        more = "" if len(errors) <= 5 else f" 等 {len(errors)} 项"
        msg += f"<br><small>部分文档未入索引：{escaped}{more}</small>"
    return msg

GLOBAL_CSS = """
<style>
:root {
    --brand-primary:#1458d6;
    --brand-primary-soft:#3c7bff;
    --brand-primary-ghost:rgba(20,88,214,0.08);
    --brand-bg:#f1f5fb;
    --brand-card:#ffffff;
    --brand-text:#1f2937;
    --brand-muted:#64748b;
    --brand-border:rgba(20,88,214,0.16);
    --brand-shadow:0 18px 42px rgba(20,88,214,0.15);
    --brand-radius:22px;
}
body, body * {
    font-family:"PingFang SC","Microsoft YaHei","Source Han Sans SC","Helvetica Neue",Arial,sans-serif !important;
    color:var(--brand-text);
}
body {
    background:linear-gradient(160deg,#eef3fc 0%,#f8fbff 55%,#ffffff 100%);
}
.gradio-container {
    background:transparent !important;
    max-width:1880px;
    width:100%;
    margin:0 auto;
    padding:16px 20px 56px;
}
.gradio-container .block.padded {
    background:transparent;
    border:none;
    box-shadow:none;
}
.gradio-container .prose h1,
.gradio-container .prose h2,
.gradio-container .prose h3 {
    color:var(--brand-text);
    font-weight:600;
}
.gradio-container .prose a { color:var(--brand-primary); }
.gradio-container .prose code {
    font-family:"Fira Code","JetBrains Mono","SFMono-Regular",Consolas,monospace;
    background:var(--brand-primary-ghost);
    padding:2px 6px;
    border-radius:6px;
}
.gradio-container button {
    border-radius:999px !important;
    font-weight:600;
    transition:transform .2s ease,box-shadow .2s ease;
}
.gradio-container button.primary,
.gradio-container button[aria-label="搜索"],
.gradio-container button[aria-label="刷新树"] {
    background:linear-gradient(135deg,var(--brand-primary),var(--brand-primary-soft));
    border:none;
    color:#fff;
}
.gradio-container button.primary:hover,
.gradio-container button[aria-label="搜索"]:hover,
.gradio-container button[aria-label="刷新树"]:hover {
    transform:translateY(-1px);
    box-shadow:0 12px 26px rgba(20,88,214,0.25);
}
.mkv-topbar {
    display:flex;
    align-items:center;
    justify-content:space-between;
    background:var(--brand-card);
    border-radius:var(--brand-radius);
    border:1px solid var(--brand-border);
    padding:18px 26px;
    box-shadow:var(--brand-shadow);
    position:sticky;
    top:12px;
    z-index:10;
}
.mkv-brand {
    display:flex;
    align-items:center;
    gap:14px;
}
.mkv-logo {
    width:46px;
    height:46px;
    border-radius:14px;
    background:linear-gradient(135deg,var(--brand-primary),var(--brand-primary-soft));
    display:flex;
    align-items:center;
    justify-content:center;
    color:#fff;
    font-weight:700;
    font-size:1.2rem;
    letter-spacing:.02em;
    box-shadow:0 12px 22px rgba(20,88,214,0.28);
}
.mkv-brand-title {
    font-size:1.32rem;
    font-weight:700;
}
.mkv-brand-subtitle {
    margin-top:2px;
    font-size:.92rem;
    color:var(--brand-muted);
}
.mkv-links {
    display:flex;
    align-items:center;
    gap:16px;
    font-size:.95rem;
}
.mkv-link {
    color:var(--brand-primary);
    font-weight:600;
    text-decoration:none;
    padding:8px 16px;
    border-radius:999px;
    background:var(--brand-primary-ghost);
    transition:all .2s ease;
}
.mkv-link:hover {
    color:#fff;
    background:linear-gradient(135deg,var(--brand-primary),var(--brand-primary-soft));
    box-shadow:0 12px 26px rgba(20,88,214,0.2);
}
.mkv-hero {
    margin:22px 0 18px;
    padding:28px 32px;
    border-radius:var(--brand-radius);
    background:var(--brand-card);
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
}
.mkv-hero h1 {
    font-size:1.8rem;
    margin-bottom:10px;
}
.mkv-hero p {
    margin:0;
    color:var(--brand-muted);
    line-height:1.6;
}
.mkv-meta-bar {
    display:flex;
    flex-wrap:wrap;
    gap:12px;
    align-items:center;
    justify-content:space-between;
    margin-top:14px;
}
.mkv-meta {
    display:flex;
    flex-wrap:wrap;
    gap:12px;
    font-size:.95rem;
}
.mkv-meta span {
    padding:6px 12px;
    border-radius:999px;
    background:var(--brand-primary-ghost);
    color:var(--brand-primary);
}
.mkv-meta-link {
    margin-left:auto;
    display:flex;
    align-items:center;
}
.mkv-meta-link .mkv-link {
    white-space:nowrap;
}
.mkv-meta-link .mkv-link + .mkv-link {
    margin-left:12px;
}
.gr-row {
    display:flex;
    flex-direction:row !important;
    flex-wrap:wrap;
    align-items:flex-start;
    gap:20px !important;
}
#layout-main {
    flex-wrap:nowrap;
    align-items:flex-start;
}
#layout-main > .sidebar-col {
    flex:0 0 304px;
    max-width:368px;
}
#layout-main > .content-col {
    flex:1 1 auto;
    min-width:0;
}
.sidebar-col {
    font-size:.92rem;
}
.sidebar-col .controls {
    display:flex;
    flex-wrap:wrap;
    margin-bottom:12px;
}
.sidebar-col .controls > * + * {
    margin-left:10px;
}
.sidebar-heading h3 {
    margin-bottom:12px !important;
    color:var(--brand-muted);
    letter-spacing:.02em;
}
.sidebar-col .gr-button { min-width:104px; }
.sidebar-col .status-bar {
    margin:6px 0 0;
    color:var(--brand-muted);
    font-size:.9rem;
}
.sidebar-col .status-bar em { color:var(--brand-muted); }
.sidebar-sticky {
    position:sticky;
    top:32px;
    display:flex;
    flex-direction:column;
    gap:14px;
    align-self:flex-start;
}
.content-col {
    flex:1 1 640px;
    min-width:0;
}
.sidebar-tree {
    padding:16px 18px;
    background:var(--brand-card);
    border-radius:var(--brand-radius);
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
    max-height:60vh;
    overflow:auto;
}
.sidebar-tree summary,
.sidebar-tree .file {
    font-size:.9rem;
}
.sidebar-tree::-webkit-scrollbar { width:8px; }
.sidebar-tree::-webkit-scrollbar-thumb {
    background:rgba(20,88,214,0.25);
    border-radius:10px;
}
.sidebar-tree details { margin-left:.6rem; }
.sidebar-tree summary {
    cursor:pointer;
    padding:4px 8px;
    border-radius:10px;
    color:var(--brand-muted);
    transition:background .2s ease,color .2s ease;
}
.sidebar-tree summary:hover {
    background:rgba(20,88,214,0.12);
    color:var(--brand-primary);
}
.sidebar-tree .file {
    padding:4px 8px;
    border-radius:9px;
    color:var(--brand-text);
    transition:background .2s ease;
}
.sidebar-tree .file:hover {
    background:rgba(20,88,214,0.12);
}
.sidebar-tree .file a {
    color:var(--brand-primary);
    text-decoration:none;
    font-weight:500;
}
.sidebar-tree .file a:hover { text-decoration:underline; }
.search-input textarea,
.search-input input {
    border-radius:16px !important;
    border:1px solid var(--brand-border) !important;
    background:#f8fbff !important;
    padding:10px 16px !important;
    font-size:.9rem !important;
    box-shadow:none !important;
}
.search-title {
    font-weight:600;
    color:var(--brand-muted);
    margin:6px 0 4px;
    font-size:.88rem;
    letter-spacing:.02em;
}
.search-stack {
    display:flex;
    flex-direction:column;
    gap:10px;
}
.search-button button {
    width:100%;
    border-radius:16px !important;
    padding:10px 0 !important;
}
.reindex-stack {
    display:flex;
    flex-direction:column;
    margin-top:8px;
}
.reindex-stack > * + * {
    margin-top:8px;
}
.reindex-stack .gr-button {
    align-self:flex-start;
    min-width:130px;
}
.content-col {
    display:flex;
    flex-direction:column;
    gap:18px;
}
.content-card {
    background:var(--brand-card);
    border-radius:var(--brand-radius);
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
    padding:18px 24px;
}
.toc-col {
    position:sticky;
    top:126px;
    display:flex;
    flex-direction:column;
    gap:12px;
    align-self:flex-start;
}
.toc-heading h3 {
    margin-bottom:12px !important;
    color:var(--brand-muted);
}
.toc-card {
    background:var(--brand-card);
    border-radius:var(--brand-radius);
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
    padding:18px 20px;
    max-height:72vh;
    overflow:auto;
}
.toc-card::-webkit-scrollbar { width:8px; }
.toc-card::-webkit-scrollbar-thumb {
    background:rgba(20,88,214,0.25);
    border-radius:10px;
}
.toc-tree {
    font-size:.95rem;
    line-height:1.6;
}
.toc-tree > .toc-list { padding-left:0; }
.toc-tree ul {
    list-style:none;
    padding-left:1.1rem;
    margin:6px 0;
}
.toc-tree li { margin:4px 0; }
.toc-tree a {
    color:var(--brand-primary);
    text-decoration:none;
    font-weight:500;
}
.toc-tree a:hover { text-decoration:underline; }
.toc-empty {
    color:var(--brand-muted);
    font-size:.95rem;
    line-height:1.6;
}
.download-panel {
    margin-bottom:12px;
}
.download-actions {
    display:flex;
    justify-content:flex-start;
}
.download-button {
    display:inline-flex;
    align-items:center;
    gap:6px;
    padding:10px 18px;
    border-radius:14px;
    background:var(--brand-primary);
    color:#ffffff !important;
    font-weight:600;
    text-decoration:none;
    box-shadow:var(--brand-shadow);
    transition:transform .15s ease, box-shadow .15s ease;
}
.download-button:hover {
    transform:translateY(-1px);
    box-shadow:0 10px 20px rgba(20,88,214,0.2);
}
.doc-preview,
.plaintext-view textarea {
    width:100%;
    border-radius:18px !important;
    border:1px solid var(--brand-border) !important;
    background:#ffffff !important;
    box-shadow:var(--brand-shadow);
}
.doc-preview {
    padding:20px 22px;
    margin:0;
    line-height:1.72;
    font-size:1rem;
    box-sizing:border-box;
}
.doc-preview-inner {
    min-height:1rem;
}
.doc-preview-empty {
    color:var(--brand-muted);
}
.mathjax-error-banner {
    margin-bottom:16px;
    padding:12px 16px;
    border-radius:14px;
    background:rgba(220, 38, 38, 0.12);
    border:1px solid rgba(220, 38, 38, 0.22);
    color:#991b1b;
    font-size:.95rem;
    line-height:1.6;
}
.plaintext-view textarea {
    min-height:420px !important;
    font-family:"Fira Code","JetBrains Mono","SFMono-Regular",Consolas,monospace !important;
    font-size:.92rem !important;
    color:#0f172a !important;
}
.gradio-container .tab-nav button {
    font-weight:600;
    padding:10px 18px;
    border-radius:999px;
}
.gradio-container .tab-nav button[aria-selected="true"] {
    color:var(--brand-primary);
    background:var(--brand-primary-ghost);
}
.search-panel {
    padding:18px 22px;
    background:var(--brand-card);
    border-radius:var(--brand-radius);
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
}
.search-panel mark {
    background:rgba(20,88,214,0.18);
    color:var(--brand-text);
    border-radius:4px;
    padding:0 3px;
}
.search-snippet {
    margin-left:1.2rem;
    color:#334155;
    font-size:.94rem;
}
.search-snippet mark {
    background:rgba(20,88,214,0.18);
    color:var(--brand-text);
    border-radius:4px;
    padding:0 3px;
}
.badge {
    font-size:.82rem;
    color:var(--brand-muted);
}
.doc-error {
    margin-top:.6rem;
    padding:14px 18px;
    border-radius:14px;
    background:rgba(239,68,68,0.12);
    color:#b91c1c;
    border:1px solid rgba(239,68,68,0.25);
}
.markdown-body table {
    border-collapse:collapse;
    width:100%;
}
.markdown-body th,
.markdown-body td {
    border:1px solid rgba(20,88,214,0.18);
    padding:6px 10px;
}
.markdown-body blockquote {
    border-left:4px solid rgba(20,88,214,0.25);
    margin-left:0;
    padding-left:12px;
    color:var(--brand-muted);
}
.markdown-body .arithmatex {
    font-size:1em;
}
.markdown-body mjx-container[jax="CHTML"] {
    font-size:1em;
}
.markdown-body mjx-container[jax="CHTML"][display="true"] {
    margin:1.2em 0 !important;
}
@media (max-width:1100px) {
    .gradio-container {
        padding:12px 18px 40px;
    }
    .mkv-topbar {
        flex-direction:column;
        gap:12px;
        align-items:flex-start;
    }
    .sidebar-sticky { position:static; }
    .sidebar-tree { max-height:unset; }
    .toc-col { position:static; }
    .toc-card { max-height:unset; }
}
@media (max-width:768px) {
    .gradio-container { padding:10px 12px 32px; }
    .gr-row { flex-direction:column !important; }
    #layout-main { flex-wrap:wrap; }
    #layout-main > .sidebar-col,
    #layout-main > .content-col {
        max-width:none;
        flex:1 1 auto;
    }
}
</style>
"""

TREE_CSS = """
<style>
.markdown-body table {
    border-collapse:collapse;
    width:100%;
}
.markdown-body th,
.markdown-body td {
    border:1px solid rgba(20,88,214,0.18);
    padding:6px 10px;
}
.markdown-body blockquote {
    border-left:4px solid rgba(20,88,214,0.25);
    margin-left:0;
    padding-left:12px;
    color:var(--brand-muted);
}
.markdown-body .arithmatex {
    font-size:1em;
}
.markdown-body mjx-container[jax="CHTML"] {
    font-size:1em;
}
.markdown-body mjx-container[jax="CHTML"][display="true"] {
    margin:1.2em 0 !important;
}
</style>
"""

# ==================== 全文搜索 ====================

def make_snippet(text: str, q: str, width: int = 60) -> str:
    t = text
    ql = q.lower()
    tl = t.lower()
    pos = tl.find(ql)
    if pos < 0:
        return _esc(t[:width*2] + ("…" if len(t) > width*2 else ""))
    a = max(0, pos - width)
    b = min(len(t), pos + len(q) + width)
    snippet = t[a:b]
    # 简单高亮（大小写不敏感）
    snippet_html = _esc(snippet)
    pat = re.compile(re.escape(q), re.IGNORECASE)
    snippet_html = pat.sub(lambda m: f"<mark>{_esc(m.group(0))}</mark>", snippet_html)
    return ("…" if a>0 else "") + snippet_html + ("…" if b<len(t) else "")


def fulltext_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "<em>请输入关键字</em>"
    if not ES_ENABLED:
        return "<em>未配置 Elasticsearch，无法执行全文检索</em>"
    try:
        es = es_connect()
    except Exception as exc:  # pragma: no cover - 运行时依赖外部服务
        return f"<em>搜索服务不可用：{_esc(str(exc))}</em>"
    try:
        search_body = {
            "size": 200,
            # 使用 match 查询与 Postman 中保持一致，避免 multi_match 在仅有单字段时出现兼容性问题
            "query": {"match": {"content": {"query": query}}},
            "highlight": {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "fields": {"content": {"fragment_size": 120, "number_of_fragments": 3}},
                "max_analyzed_offset": ES_MAX_ANALYZED_OFFSET,
            },
        }
        resp = _es_search_request(
            es,
            search_body,
            params={"max_analyzed_offset": ES_MAX_ANALYZED_OFFSET},
        )
    except NotFoundError:
        return "<em>索引尚未建立，请先同步文档</em>"
    except Exception as exc:  # pragma: no cover - 运行时依赖外部服务
        return f"<em>检索失败：{_esc(str(exc))}</em>"
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return "<em>未找到匹配内容</em>"
    rows: List[str] = []
    for hit in hits:
        key = hit.get("_id") or ""
        title = key.split("/")[-1] if key else "未知文件"
        highlights = hit.get("highlight", {}).get("content", [])
        if highlights:
            snippet = "<br>".join(highlights)
        else:
            src = hit.get("_source", {})
            snippet = make_snippet(src.get("content", ""), query)
        score = hit.get("_score", 0.0)
        icon = _file_icon(key or title)
        rows.append(
            f"<div>{icon} <a href='?{urlencode({'key': key})}'>{_esc(title)}</a> "
            f"<span class='badge'>(相关度 {score:.2f})</span><br>"
            f"<div class='search-snippet'>{snippet}</div></div>"
        )
    return "".join(rows)

# ==================== 预签名下载链接 ====================

def download_link_html(key: str) -> str:
    c, ep = connect()
    url = c.presigned_get_object(DOC_BUCKET, key, expires=timedelta(hours=6))
    esc = _esc(url)
    return (
        "<div class='download-actions'>"
        f"<a class='download-button' href='{esc}' target='_blank' rel='noopener'>下载当前文件（有效 6 小时）</a>"
        "</div>"
    )

# ==================== Gradio UI ====================


def _hero_html(doc_total: Optional[int] = None) -> str:
    if doc_total is None:
        total_span = "<span>文档总数统计中…</span>"
    else:
        total_span = f"<span>文档总数：<strong>{doc_total}</strong></span>"
    meta_items = [total_span]
    feedback_links = [
        (
            "<a class='mkv-link mkv-feedback-link' href='http://10.20.41.24:9001/' "
            "target='_blank' rel='noopener'>文档问题反馈</a>"
        ),
        (
            "<a class='mkv-link mkv-feedback-link' href='http://10.20.40.101:7860/' "
            "target='_blank' rel='noopener'>通号院在线扫描类 PDF 解析工具</a>"
        ),
    ]
    feedback_link = "".join(feedback_links)
    return (
        f"""
        <section class='mkv-hero'>
            <h1>{_esc(SITE_TITLE)}</h1>
            <p>在这里浏览、检索来自通号院的知识文档，快速定位你需要的工作内容。</p>
            <div class='mkv-meta-bar'>
                <div class='mkv-meta'>{''.join(meta_items)}</div>
                <div class='mkv-meta-link'>{feedback_link}</div>
            </div>
        </section>
        """
    )


def _manifest_payload() -> Dict[str, object]:
    short_name = SITE_TITLE if len(SITE_TITLE) <= 12 else SITE_TITLE[:12]
    return {
        "name": SITE_TITLE,
        "short_name": short_name,
        "start_url": ".",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#1458d6",
        "icons": [],
    }


def ui_app():
    with gr.Blocks(
        title=SITE_TITLE,
        theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate"),
        head=MATHJAX_HEAD + LAYOUT_FALLBACK_HEAD,
    ) as demo:
        gr.HTML(GLOBAL_CSS + TREE_CSS)
        hero_html = gr.HTML(_hero_html())
        with gr.Row(elem_classes=["gr-row"], elem_id="layout-main"):
            with gr.Column(scale=1, min_width=280, elem_classes=["sidebar-col"]):
                with gr.Column(elem_classes=["sidebar-sticky"]):
                    gr.Markdown("### 📁 文档目录", elem_classes=["sidebar-heading"])
                    with gr.Row(elem_classes=["controls"]):
                        btn_expand = gr.Button("展开全部")
                        btn_collapse = gr.Button("折叠全部")
                    with gr.Column(elem_classes=["search-stack"]):
                        q = gr.Textbox(
                            show_label=False,
                            placeholder="支持全文搜索",
                            elem_classes=["search-input"],
                        )
                        btn_search = gr.Button("搜索", elem_classes=["search-button"])
                    tree_html = gr.HTML("<em>加载中…</em>", elem_classes=["sidebar-tree"])
                    with gr.Row(elem_classes=["controls"]):
                        btn_clear = gr.Button("清空缓存")
                        btn_refresh = gr.Button("刷新树", variant="secondary")
                    with gr.Column(elem_classes=["reindex-stack"]):
                        btn_reindex = gr.Button("重建索引", variant="secondary")
                        status_bar = gr.HTML("", elem_classes=["status-bar"])
            with gr.Column(scale=7, elem_classes=["content-col"]):
                with gr.Tabs(selected="preview", elem_id="content-tabs", elem_classes=["content-card"]) as content_tabs:
                    with gr.TabItem("目录", id="toc"):
                        toc_panel = gr.HTML(
                            "<div class='toc-empty'>请选择 Markdown 文档以生成目录</div>",
                            elem_classes=["toc-card"],
                        )
                    with gr.TabItem("预览", id="preview"):
                        dl_html = gr.HTML("", elem_classes=["download-panel"])
                        html_view = gr.HTML(
                            "<div class='doc-preview-inner doc-preview-empty'><em>请选择左侧文件…</em></div>",
                            elem_id="doc-html-view",
                            elem_classes=["doc-preview"],
                        )
                    with gr.TabItem("文本内容", id="source"):
                        md_view = gr.Textbox(lines=26, interactive=False, label="提取的纯文本", elem_classes=["plaintext-view"])
                    with gr.TabItem("全文搜索", id="search"):
                        search_out = gr.HTML(
                            "<em>在左侧输入关键词后点击“搜索”（由 Elasticsearch 提供支持）</em>",
                            elem_classes=["search-panel"],
                        )

        # 内部状态：是否展开全部
        expand_state = gr.State(True)

        def _refresh_tree(expand_all: bool):
            global TREE_DOCS
            docs = list_documents()
            TREE_DOCS = docs
            base = DOC_PREFIX.rstrip("/") + "/" if DOC_PREFIX else ""
            tree = build_tree([d["key"] for d in docs], base_prefix=base)
            status = sync_elasticsearch(docs)
            return render_tree_html(tree, expand_all), status, _hero_html(len(docs))

        def _render_cached_tree(expand_all: bool):
            global TREE_DOCS
            if not TREE_DOCS:
                return _refresh_tree(expand_all)
            base = DOC_PREFIX.rstrip("/") + "/" if DOC_PREFIX else ""
            tree = build_tree([d["key"] for d in TREE_DOCS], base_prefix=base)
            return render_tree_html(tree, expand_all), gr.update(), gr.update()

        def _render_from_key(key: str | None):
            if not key:
                return "", "<em>未选择文件</em>", "", "<div class='toc-empty'>请选择 Markdown 文档以生成目录</div>"
            try:
                _, doc_type, text, html, toc = get_document(key)
            except Exception as exc:
                msg = _esc(str(exc))
                return download_link_html(key), f"<div class='doc-error'>{msg}</div>", msg, "<div class='toc-empty'>无法生成目录</div>"

            if doc_type == "markdown":
                toc_html = toc or "<div class='toc-empty'>文档中暂无可用标题</div>"
            else:
                toc_html = "<div class='toc-empty'>当前文档类型未提供目录</div>"

            return download_link_html(key), html, text, toc_html

        def _search(query: str):
            return fulltext_search(query)

        def _clear_cache():
            n = len(DOC_CACHE.od)
            DOC_CACHE.clear()
            return f"<em>已清空文档缓存（{n} 项）</em>"

        def _force_reindex():
            global TREE_DOCS
            if not TREE_DOCS:
                TREE_DOCS = list_documents()
            return sync_elasticsearch(TREE_DOCS, force=True)

        def _activate_search_tab():
            return gr.update(selected="search")

        # 事件绑定
        demo.load(lambda: _refresh_tree(True), outputs=[tree_html, status_bar, hero_html])
        btn_refresh.click(_refresh_tree, inputs=expand_state, outputs=[tree_html, status_bar, hero_html])
        btn_expand.click(lambda: True, None, expand_state).then(_render_cached_tree, inputs=expand_state, outputs=[tree_html, status_bar, hero_html])
        btn_collapse.click(lambda: False, None, expand_state).then(_render_cached_tree, inputs=expand_state, outputs=[tree_html, status_bar, hero_html])
        btn_clear.click(_clear_cache, outputs=status_bar)
        btn_reindex.click(_force_reindex, outputs=status_bar)

        q.submit(_search, inputs=q, outputs=search_out).then(_activate_search_tab, outputs=content_tabs)
        btn_search.click(_search, inputs=q, outputs=search_out).then(_activate_search_tab, outputs=content_tabs)

        # 解析 URL 参数中的 key 并渲染
        def on_load_with_req(request: gr.Request):
            key = request.query_params.get("key") if request and request.query_params else None
            return _render_from_key(key)

        demo.load(on_load_with_req, outputs=[dl_html, html_view, md_view, toc_panel])
    return demo

if __name__ == "__main__":
    demo = ui_app()
    if ENABLE_GRADIO_QUEUE:
        demo = demo.queue()
    app = demo
    fastapi_app = demo.app

    @fastapi_app.get("/manifest.json")
    def manifest_route():  # pragma: no cover - FastAPI integration
        return JSONResponse(_manifest_payload())

    demo.launch(server_name=BIND_HOST, server_port=BIND_PORT, show_api=False)
