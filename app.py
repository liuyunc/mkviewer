import os
import re
from collections import OrderedDict
from datetime import timedelta
from typing import Dict, List, Tuple
from urllib.parse import quote, urlencode

import gradio as gr
from markdown import markdown
from minio import Minio

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

# ==================== 图片链接重写 ====================
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")

def _to_public_image_url(path: str) -> str:
    p = path.strip().lstrip("./").lstrip("/")
    parts = [quote(seg) for seg in p.split("/")]
    return IMAGE_PUBLIC_BASE.rstrip("/") + "/" + "/".join(parts)

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

MD_CACHE = LRU(512)  # key -> (etag, text, html)

# ==================== 列表/读取 ====================

def list_md_files() -> List[str]:
    c, _ = connect()
    objs = c.list_objects(DOC_BUCKET, prefix=DOC_PREFIX or None, recursive=True)
    out: List[str] = []
    for o in objs:
        name = o.object_name
        if name.lower().endswith((".md", ".markdown")):
            out.append(name)
    return sorted(out, key=str.lower)


def get_md(key: str) -> Tuple[str, str, str]:
    """返回 (etag, text, html)；缓存命中则不再下载。"""
    c, _ = connect()
    stat = c.stat_object(DOC_BUCKET, key)
    etag = getattr(stat, "etag", None) or getattr(stat, "_etag", None) or ""
    cached = MD_CACHE.get(key)
    if cached and cached[0] == etag:
        return cached
    resp = c.get_object(DOC_BUCKET, key)
    data = resp.read(); resp.close(); resp.release_conn()
    text = data.decode("utf-8", errors="ignore")
    text2 = rewrite_image_links(text)
    html = markdown(text2, extensions=["fenced_code", "tables", "codehilite"])
    MD_CACHE.set(key, (etag, text, html))
    return etag, text, html

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
            html.append(f"<div class='file'>📝 <a href='{link}'>{_esc(name)}</a></div>")
    rec(tree)
    return "".join(html) if html else "<em>没有找到 Markdown 文件</em>"

GLOBAL_CSS = """
<style>
:root {
    --brand-primary:#1f6feb;
    --brand-primary-light:#4c8dff;
    --brand-bg:#f4f7ff;
    --brand-card:#ffffff;
    --brand-text:#1f2933;
    --brand-muted:#5f6c7d;
    --brand-border:rgba(31,111,235,0.18);
    --brand-shadow:0 16px 40px rgba(31,111,235,0.12);
}
body, body * {
    font-family:"PingFang SC","Microsoft YaHei","Source Han Sans SC","Helvetica Neue",Arial,sans-serif !important;
    color:var(--brand-text);
}
body { background:var(--brand-bg); }
.gradio-container { background:transparent !important; }
.gradio-container .prose h1,
.gradio-container .prose h2,
.gradio-container .prose h3 {
    color:var(--brand-text);
    font-weight:600;
}
.gradio-container .prose a { color:var(--brand-primary); }
.gradio-container .prose code { font-family:"Fira Code","JetBrains Mono","SFMono-Regular",Consolas,monospace; }
.gradio-container button {
    border-radius:999px !important;
    font-weight:600;
}
.gradio-container button.primary,
.gradio-container button[aria-label="搜索"],
.gradio-container button[aria-label="刷新树"] {
    background:linear-gradient(135deg,var(--brand-primary),var(--brand-primary-light));
    border:none;
}
.gradio-container button.primary:hover,
.gradio-container button[aria-label="搜索"]:hover,
.gradio-container button[aria-label="刷新树"]:hover {
    filter:brightness(1.05);
}
.gradio-container .block.padded {
    background:var(--brand-card);
    border-radius:18px;
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
}
.mkv-header {
    padding:18px 22px;
    margin-bottom:12px;
    background:var(--brand-card);
    border-radius:20px;
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
}
.mkv-header h1 {
    font-size:1.6rem;
    margin-bottom:.2rem;
}
.mkv-header p {
    margin:0;
    color:var(--brand-muted);
}
.controls {
    display:flex;
    gap:10px;
    flex-wrap:wrap;
}
.controls .gr-button {
    min-width:96px;
}
.sidebar {
    position:sticky;
    top:8px;
    max-height:82vh;
    overflow:auto;
    padding:14px 16px;
    background:linear-gradient(180deg,rgba(76,141,255,0.12),rgba(255,255,255,0.9));
    border:1px solid var(--brand-border);
    border-radius:18px;
    box-shadow:var(--brand-shadow);
}
.sidebar::-webkit-scrollbar {
    width:8px;
}
.sidebar::-webkit-scrollbar-thumb {
    background:rgba(31,111,235,0.28);
    border-radius:10px;
}
.sidebar details { margin-left:.4rem; }
.sidebar summary {
    cursor:pointer;
    padding:4px 8px;
    border-radius:10px;
    color:var(--brand-muted);
}
.sidebar summary:hover {
    background:rgba(31,111,235,0.12);
    color:var(--brand-primary);
}
.file {
    padding:4px 8px;
    border-radius:8px;
    color:var(--brand-text);
}
.file:hover {
    background:rgba(31,111,235,0.12);
}
.file a {
    color:var(--brand-primary);
    text-decoration:none;
    font-weight:500;
}
.file a:hover {
    text-decoration:underline;
}
.badge {
    font-size:.82rem;
    color:var(--brand-muted);
}
.search-panel {
    padding:12px 16px;
    background:var(--brand-card);
    border-radius:16px;
    border:1px solid var(--brand-border);
    box-shadow:var(--brand-shadow);
}
.search-panel mark {
    background:rgba(31,111,235,0.2);
    color:var(--brand-text);
    border-radius:4px;
    padding:0 2px;
}
.gradio-container .tab-nav button {
    font-weight:600;
}
.gradio-container .tab-nav button[aria-selected="true"] {
    color:var(--brand-primary);
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
    border:1px solid rgba(31,111,235,0.12);
    padding:6px 10px;
}
.markdown-body blockquote {
    border-left:4px solid rgba(31,111,235,0.25);
    margin-left:0;
    padding-left:12px;
    color:var(--brand-muted);
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
    files = list_md_files()
    results: List[Tuple[str,int,str]] = []  # (key, score, snippet)
    ql = query.lower()
    for k in files:
        _, text, _ = get_md(k)
        cnt = text.lower().count(ql)
        if cnt > 0:
            results.append((k, cnt, make_snippet(text, query)))
    if not results:
        return "<em>未找到匹配内容</em>"
    results.sort(key=lambda x: (-x[1], x[0].lower()))
    rows = [f"<div>🔎 <a href='?{urlencode({'key':k})}'>{_esc(k.split('/')[-1])}</a> <span class='badge'>(匹配 {score} 次)</span><br><div style='margin-left:1.2rem;color:#374151;font-size:.9rem'>{snip}</div></div>" for k,score,snip in results[:200]]
    return "".join(rows)

# ==================== 预签名下载链接 ====================

def download_link_html(key: str) -> str:
    c, ep = connect()
    url = c.presigned_get_object(DOC_BUCKET, key, expires=timedelta(hours=6))
    esc = _esc(url)
    return f"<div style='margin:8px 0;'>🔗 <a href='{esc}' target='_blank' rel='noopener'>下载当前文件（有效 6 小时）</a><br><small>或复制：<code>{esc}</code></small></div>"

# ==================== Gradio UI ====================

def ui_app():
    with gr.Blocks(title=SITE_TITLE, theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate")) as demo:
        gr.HTML(GLOBAL_CSS + TREE_CSS)
        gr.HTML(
            f"<div class='mkv-header'><h1>{_esc(SITE_TITLE)}</h1>"
            f"<p>Endpoint：<strong>{_esc(', '.join(MINIO_ENDPOINTS))}</strong> · 文档桶：<strong>{_esc(DOC_BUCKET)}</strong> · 前缀：<strong>{_esc(DOC_PREFIX or '/')}</strong></p></div>"
        )
        with gr.Row():
            with gr.Column(scale=1, min_width=340):
                gr.Markdown("### 📁 文档目录")
                with gr.Row(elem_classes=["controls"]):
                    btn_refresh = gr.Button("刷新树", variant="secondary")
                    btn_expand = gr.Button("展开全部")
                    btn_collapse = gr.Button("折叠全部")
                    btn_clear = gr.Button("清空缓存")
                q = gr.Textbox(label="全文搜索", placeholder="输入关键字… 然后回车或点搜索")
                btn_search = gr.Button("搜索")
                tree_html = gr.HTML("<em>加载中…</em>", elem_classes=["sidebar"])
            with gr.Column(scale=4):
                with gr.Tabs(selected="preview", elem_id="content-tabs") as content_tabs:
                    with gr.TabItem("预览", id="preview"):
                        dl_html = gr.HTML("")
                        html_view = gr.HTML("<em>请选择左侧文件…</em>")
                    with gr.TabItem("源文件", id="source"):
                        md_view = gr.Textbox(lines=26, interactive=False)
                    with gr.TabItem("全文搜索", id="search"):
                        search_out = gr.HTML("<em>在左侧输入关键词后点击“搜索”</em>", elem_classes=["search-panel"])

        # 内部状态：是否展开全部
        expand_state = gr.State(True)

        def _load_tree(expand_all: bool):
            files = list_md_files()
            base = DOC_PREFIX.rstrip("/") + "/" if DOC_PREFIX else ""
            t = build_tree(files, base_prefix=base)
            return render_tree_html(t, expand_all)

        def _render_from_key(key: str | None):
            if not key:
                return "", "<em>未选择文件</em>", ""
            _, text, html = get_md(key)
            return download_link_html(key), html, text

        def _search(query: str):
            return fulltext_search(query)

        def _clear_cache():
            n = len(MD_CACHE.od)
            MD_CACHE.clear()
            return f"<em>已清空缓存（{n} 项）</em>"

        def _activate_search_tab():
            return gr.Tabs.update(selected="search")

        # 事件绑定
        demo.load(lambda: _load_tree(True), outputs=tree_html)
        btn_refresh.click(lambda e: _load_tree(True), outputs=tree_html)
        btn_expand.click(lambda: True, outputs=expand_state).then(_load_tree, inputs=expand_state, outputs=tree_html)
        btn_collapse.click(lambda: False, outputs=expand_state).then(_load_tree, inputs=expand_state, outputs=tree_html)

        q.submit(_search, inputs=q, outputs=search_out).then(_activate_search_tab, outputs=content_tabs)
        btn_search.click(_search, inputs=q, outputs=search_out).then(_activate_search_tab, outputs=content_tabs)

        # 解析 URL 参数中的 key 并渲染
        def on_load_with_req(request: gr.Request):
            key = request.query_params.get("key") if request and request.query_params else None
            return _render_from_key(key)

        demo.load(on_load_with_req, outputs=[dl_html, html_view, md_view])
    return demo

if __name__ == "__main__":
    app = ui_app()
    app.queue().launch(server_name=BIND_HOST, server_port=BIND_PORT, show_api=False)
