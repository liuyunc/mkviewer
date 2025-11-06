# MKViewer 架构总览

本文面向需要维护或二次开发 MKViewer 的工程师，梳理整个应用的结构、核心组件以及典型的数据流转流程，便于快速理解系统边界与扩展点。

## 系统组件一览

- **配置层**：应用启动即从环境变量装载 MinIO、Elasticsearch、UI 等参数，并对兼容性与默认值进行兜底处理，确保不同部署环境下行为一致。【F:app.py†L1-L71】
- **存储访问层**：通过懒加载的 `connect()` 维护与 MinIO 的单例连接，遇到多个节点时会按顺序重试并缓存成功的客户端，避免每次请求都重新握手。【F:app.py†L377-L395】
- **搜索访问层**：`es_connect()` 构造兼容 Elasticsearch 7/8 的客户端，必要时注入兼容头封装，后续所有检索与索引操作都复用这一实例。【F:app.py†L396-L480】
- **文档处理管线**：`list_documents()` 和 `get_document()` 负责扫描对象存储、补齐原始 PDF 映射、按类型渲染 Markdown/DOCX/DOC，输出文本、HTML 及目录信息。【F:app.py†L805-L953】
- **缓存层**：以 ETag 为键的 LRU 缓存避免重复解码大文件，并允许通过 UI 一键清空，保障频繁访问时的性能。【F:app.py†L800-L807】【F:app.py†L2274-L2277】
- **索引/检索层**：`sync_elasticsearch()` 负责构建/更新全文索引，`fulltext_search()` 根据搜索模式组装查询并返回高亮 HTML 结果。【F:app.py†L1044-L1116】【F:app.py†L1936-L2048】
- **前端 UI 层**：基于 Gradio Blocks 搭建的单页应用，定义了目录树、预览区、全文搜索 Tab 等交互组件，并集中注入定制样式与 MathJax 引导脚本。【F:app.py†L2143-L2299】
- **服务出口**：默认以 Gradio 启动 HTTP 服务，同时暴露 FastAPI 子应用提供 `/manifest.json` 等额外接口，可配合反向代理或 PWA 使用。【F:app.py†L2137-L2322】

## 模块结构

| 模块 | 主要职责 | 关键函数/常量 |
| ---- | -------- | ------------- |
| 配置与常量 | 统一读取运行参数、注入 MathJax 及基础样式 | 环境变量常量、`_MATHJAX_HEAD_TEMPLATE`、`LAYOUT_FALLBACK_HEAD`【F:app.py†L41-L375】 |
| MinIO 客户端 | 维护对象存储连接、生成预签名下载链接 | `connect()`、`download_link_html()`【F:app.py†L377-L395】【F:app.py†L2052-L2088】 |
| Elasticsearch 适配 | 生成兼容传输层、封装搜索请求 | `_compat_transport_class()`、`es_connect()`、`_es_search_request()`【F:app.py†L400-L712】 |
| 文档扫描与渲染 | 构建目录树、渲染 Markdown/DOCX/DOC、重写图片 | `list_documents()`、`rewrite_image_links()`、`get_document()`【F:app.py†L640-L953】 |
| 缓存/索引 | 基于 ETag 的 LRU、同步全文索引 | `LRU`、`DOC_CACHE`、`sync_elasticsearch()`【F:app.py†L792-L1116】 |
| UI 与交互 | 生成页面结构、绑定事件、处理搜索/刷新 | `ui_app()` 及内部回调 |【F:app.py†L2137-L2308】 |

## 关键数据结构

- **DOC_CACHE**：`OrderedDict` 驱动的 LRU，用于缓存 `(etag, doc_type, text, html, toc)`，容量默认 512，可通过 UI 清除。【F:app.py†L800-L807】【F:app.py†L2274-L2277】
- **TREE_DOCS / DOC_LOOKUP**：最新文档列表与键索引映射，供 UI 在多次交互间复用，减少 MinIO 列表操作。【F:app.py†L805-L808】【F:app.py†L2201-L2218】
- **文档条目**：`list_documents()` 输出包含 `key / etag / doc_type / original_key / searchable` 等字段的字典，后续渲染目录、下载与索引都依赖这些元数据。【F:app.py†L812-L873】

## 典型数据流

### 启动与初始化
1. 进程启动时按环境变量构造配置、MathJax 脚本、基础 CSS，并定义全局缓存/状态变量。【F:app.py†L41-L375】【F:app.py†L792-L809】
2. `ui_app()` 创建 Gradio Blocks，注入样式、搭建左侧导航与右侧内容区，同时注册按钮、输入框及 Tabs 的回调函数。【F:app.py†L2137-L2299】
3. 首次加载事件触发 `_refresh_tree(False)`：从 MinIO 读取文档、构建目录树、同步 Elasticsearch 索引、刷新顶部统计信息。【F:app.py†L2201-L2209】

### 目录浏览
1. `list_documents()` 列举 `DOC_BUCKET` 下支持的文档，补齐与原始 PDF 的映射并排序。【F:app.py†L812-L873】
2. `build_tree()` 将扁平路径拆分为嵌套字典，`render_tree_html()` 生成折叠目录的 HTML 结构供 Gradio 渲染。【F:app.py†L957-L1041】
3. UI 侧的“展开/折叠”按钮仅切换 `expand_state`，随后复用缓存树重新渲染，无需重新访问 MinIO。【F:app.py†L2198-L2218】【F:app.py†L2288-L2292】

### 文档预览
1. 当用户点击目录项或页面载入 URL 带 `key` 参数时，`_render_from_key()` 根据 `DOC_LOOKUP` 查找文档元数据。【F:app.py†L2220-L2237】
2. 若存在原始 PDF 或尚未数字化，直接生成下载面板并展示占位提示；否则调用 `get_document()` 拉取对象、解码文本并缓存结果。【F:app.py†L2238-L2254】【F:app.py†L884-L953】
3. Markdown 渲染过程中会重写图片链接并构建目录，DOCX/DOC 分别由 Mammoth、textract 解析，最终回传 HTML、纯文本和目录面板。【F:app.py†L903-L949】
4. MathJax 脚本通过 MutationObserver 监听预览区域变化，在文档更新后自动触发公式排版。【F:app.py†L73-L318】

### 全文搜索
1. 搜索框提交后 `_search()` 根据复选框模式选择内容或标题检索，并切换到“全文搜索”标签页。【F:app.py†L2269-L2299】
2. `fulltext_search()` 组装不同的 Bool 查询：内容模式启用短语匹配与 bool_prefix，标题模式结合 term、match 与 wildcard；若配置了高亮则在结果中插入 `<mark>` 标签。【F:app.py†L1936-L2045】
3. 结果集转为 HTML 列表，附带相关度与可点击链接，必要时回落到手动构造的上下文片段。【F:app.py†L2016-L2048】

### 索引同步
1. `_refresh_tree()` 和“重建索引”按钮都会调用 `sync_elasticsearch()`，比对现有文档与索引，剔除失效条目并增量更新变更文件。【F:app.py†L2201-L2284】【F:app.py†L1044-L1116】
2. 文档索引时会复用 `get_document()` 的缓存结果，避免二次解析，同时在完成后触发 `indices.refresh` 让变更立即可检索。【F:app.py†L1087-L1110】

### 下载链路
1. 预览区的下载按钮由 `download_link_html()` 生成，优先使用原始 PDF 桶，失败时回退到 DOC 桶，所有链接默认有效期 6 小时。【F:app.py†L2052-L2088】
2. 该流程直接调用 MinIO 客户端的预签名能力，与缓存解码逻辑解耦，便于自定义过期时间或授权策略。【F:app.py†L2052-L2088】

## 状态管理与容错

- MinIO/Elasticsearch 客户端均懒加载并在全局缓存，连接失败会抛出明确的错误信息，便于 UI 捕获后在状态栏展示。【F:app.py†L377-L395】【F:app.py†L471-L512】【F:app.py†L1049-L1116】
- `fulltext_search()`、`sync_elasticsearch()`、`get_document()` 在遇到外部依赖异常时会返回用户友好的提示语，防止将堆栈泄露到前端。【F:app.py†L1936-L2048】【F:app.py†L1044-L1116】【F:app.py†L884-L952】
- LRU 缓存可以通过按钮即时清空，且 `_force_reindex()` 会在缓存为空时重新扫描，以确保索引与 MinIO 状态一致。【F:app.py†L2274-L2283】

## 操作过程示例

### 运维部署流程
1. **准备运行环境**：根据部署方式选择虚拟环境或容器，分别执行 `pip install -r requirements.txt` 与 `python app.py`，或通过 `docker compose up -d` 一键拉起 MinIO/Elasticsearch 依赖及应用实例。【F:readme.md†L32-L80】
2. **配置环境变量**：在 `.env` 或启动脚本中写入 MinIO、Elasticsearch、文档桶与前缀等参数，确保对象存储与搜索服务可用。【F:readme.md†L66-L80】
3. **访问控制台**：浏览器打开 `http://<host>:7861`，确认左侧目录树加载完成、顶部状态栏无异常提示，首次进入会自动触发 `_refresh_tree(False)` 完成文档扫描与索引初始化。【F:app.py†L2201-L2211】【F:app.py†L2137-L2299】

### 日常使用流程
1. **浏览目录与文档**：通过左侧目录树选择文档，若命中缓存将直接返回渲染结果，否则 `get_document()` 会从 MinIO 读取对象并完成解析/缓存，同时提供原始文件下载链接。【F:app.py†L2220-L2254】【F:app.py†L884-L953】【F:app.py†L2052-L2088】
2. **执行全文搜索**：在搜索框输入关键词，选择“内容”或“标题”模式后提交，`fulltext_search()` 构造对应的 Elasticsearch 查询并将结果渲染到“全文搜索”页签。【F:app.py†L1936-L2048】【F:app.py†L2269-L2299】
3. **刷新与维护**：遇到文档有增删或渲染异常时，可点击“刷新目录”重新列举对象；需要重建索引或清理缓存时使用“重建索引”与“清空缓存”按钮，分别触发 `sync_elasticsearch()` 与 `DOC_CACHE.clear()`，确保索引与内容一致。【F:app.py†L2201-L2284】【F:app.py†L2274-L2283】

## 对外接口与部署要点

- Gradio 应用实例通过 `demo.launch()` 监听 `BIND_HOST:BIND_PORT`，可直接容器化部署或挂载到已有的 ASGI 服务器。【F:app.py†L2137-L2322】
- 内置的 FastAPI `manifest_route()` 返回 PWA 所需的基础信息，可在前端添加到桌面或移动设备中使用。【F:app.py†L2136-L2320】
- 所有样式与脚本均在 Python 侧内联定义，部署时无需额外的静态资源目录，但可通过环境变量切换 MathJax/CDN 地址以适配内外网场景。【F:app.py†L41-L375】【F:app.py†L1120-L1600】

## 扩展建议

- **接入更多文档格式**：在 `SUPPORTED_EXTS` 中注册新后缀，并于 `get_document()` 分支中实现解析逻辑，同时考虑是否参与索引。【F:app.py†L640-L953】
- **自定义索引策略**：根据业务需要调整 `sync_elasticsearch()` 中的字段映射与分词策略，或在 `fulltext_search()` 中扩展筛选条件与排序规则。【F:app.py†L1044-L1116】【F:app.py†L1936-L2048】
- **多租户/多前缀场景**：可在外部调度层为不同租户设置独立的 `DOC_PREFIX`/`ES_INDEX` 环境变量，实现逻辑隔离而无需修改代码。【F:app.py†L41-L71】【F:app.py†L2201-L2209】

