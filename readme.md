# MKViewer

轻量级的文档在线浏览器，基于 [Gradio](https://www.gradio.app/) + MinIO + Elasticsearch 组合构建，可对对象存储中的 Markdown 及 Office 文档进行在线预览与全文检索。适用于内部知识库、项目文档或需要快速搭建的知识阅读站点。

## 功能特性

- 📁 **目录树导航**：自动扫描 MinIO 中的文档（.md / .doc / .docx），以折叠目录的方式展示，支持展开/折叠、刷新与重建索引。
- 🔍 **Elasticsearch 检索**：基于 Elasticsearch 构建倒排索引，支持高亮片段与相关度排序。
- 📄 **多格式预览**：Markdown 即时渲染，DOCX 使用 Mammoth 转换为 HTML，DOC 使用 textract 提取纯文本并排版展示。
- 🖼️ **图片链接重写**：将 Markdown 中的相对图片地址转换为可访问的公网/内网地址，解决跨域访问问题。
- 📝 **实时预览与源文件查看**：预览渲染后的 HTML，同时提供原始 Markdown 文本。
- 🔗 **临时下载链接**：为当前文档生成 6 小时有效的 MinIO 预签名下载链接。
- ⚡ **缓存机制**：基于 ETag 的 LRU 缓存，减少重复请求，提高加载性能。

## 环境要求

- Python ≥ 3.10（推荐使用 3.11，与 Docker 镜像保持一致）
- MinIO 或兼容的 S3 对象存储服务
- Elasticsearch 8.x（可通过项目附带的 docker-compose 启动）
- 可选：Docker / Docker Compose 方便部署

## 快速开始

### 1. 本地运行

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\\Scripts\\activate
pip install -r requirements.txt
export MINIO_ENDPOINTS="minio.example.com:9000"
export MINIO_ACCESS_KEY="your-access-key"
export MINIO_SECRET_KEY="your-secret-key"
export ES_HOSTS="http://localhost:9200"
python app.py
```

启动后默认监听 `http://0.0.0.0:7861`。

### 2. 使用 Docker

```bash
docker build -t mkviewer .
docker run -d \
  --name mkviewer \
  -p 7861:7861 \
  -e MINIO_ENDPOINTS="minio.example.com:9000" \
  -e MINIO_ACCESS_KEY="your-access-key" \
  -e MINIO_SECRET_KEY="your-secret-key" \
  -e ES_HOSTS="http://your-es:9200" \
  mkviewer
```

### 3. 使用 Docker Compose

1. 新建 `.env` 文件，填入所需环境变量：

   ```env
   MINIO_ENDPOINTS=minio.example.com:9000
   MINIO_ACCESS_KEY=your-access-key
   MINIO_SECRET_KEY=your-secret-key
   DOC_BUCKET=docs
   DOC_PREFIX=wiki/
   IMAGE_PUBLIC_BASE=https://static.example.com/images
   SITE_TITLE=内部知识库
   ```

2. 启动服务：

   ```bash
   docker compose up -d
   ```

   > 示例 `docker-compose.yml` 同时启动单节点 Elasticsearch（默认关闭安全认证），应用会自动指向 `http://elasticsearch:9200`。

## 配置说明

| 环境变量 | 默认值 | 说明 |
| -------- | ------ | ---- |
| `MINIO_ENDPOINTS` | `10.20.41.24:9005,10.20.40.101:9005` | MinIO 集群节点列表，多个节点用逗号分隔。 |
| `MINIO_SECURE` | `false` | 是否使用 HTTPS 连接 MinIO。 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 空 | MinIO 访问凭证。 |
| `DOC_BUCKET` | `bucket` | 存放 Markdown 文档的桶名称。 |
| `DOC_PREFIX` | 空 | 文档所在的路径前缀，可用于限定子目录。 |
| `IMAGE_PUBLIC_BASE` | `http://10.20.41.24:9005/images` | 用于重写 Markdown 图片链接的公共访问地址。 |
| `ES_HOSTS` | `http://localhost:9200` | Elasticsearch 节点列表，多个节点用逗号分隔。 |
| `ES_INDEX` | `mkviewer-docs` | 全文索引名称，可自定义。 |
| `ES_USERNAME` / `ES_PASSWORD` | 空 | 访问 Elasticsearch 所需的 Basic Auth 凭证。 |
| `ES_VERIFY_CERTS` | `true` | 是否校验证书（HTTPS 环境建议保持 `true`）。 |
| `ES_TIMEOUT` | `10` | 与 Elasticsearch 通信的超时时间（秒）。 |
| `SITE_TITLE` | `通号院文档知识库` | 页面标题及顶部提示信息。 |
| `BIND_HOST` | `0.0.0.0` | 服务绑定的主机地址。 |
| `BIND_PORT` | `7861` | 服务监听端口。 |
| `MATHJAX_JS_URL` | `http://10.20.41.24:9005/cdn/mathjax@3/es5/tex-mml-chtml.js` | 数学公式渲染脚本地址，可切换为内网镜像以提升首屏渲染稳定性。 |

> 初次加载或点击“重建索引”按钮将把最新文档同步到 Elasticsearch，无法解析的文件会在页面状态栏提示。
> 其他在 `app.py` 中定义的常量也可通过环境变量覆盖。

## 使用说明

1. 打开浏览器访问部署地址，左侧为目录树与搜索栏，右侧提供“预览 / 文本内容 / 全文搜索”三个标签页。
2. 选择任意支持的文档（Markdown / DOCX / DOC）后，系统会自动渲染内容，并提供临时下载链接。
3. 使用左侧的控制按钮可刷新目录、清空缓存或执行“重建索引”将最新文档同步到 Elasticsearch。
4. 全文搜索由 Elasticsearch 提供支持，默认按照相关度排序，并对命中片段进行高亮。

## 开发与调试

- 代码主入口：[`app.py`](app.py)
- 主要依赖：`gradio`、`minio`、`Markdown`、`Pygments`、`elasticsearch`、`mammoth`、`textract`
- 样式与 UI 控制均在 `ui_app()` 中定义，可根据需求自行扩展。
- 如需调整缓存策略，可修改 `DOC_CACHE = LRU(512)` 的容量或实现。

启动开发服务器后，修改代码会在 Gradio 中自动生效；若涉及依赖更新，需重启进程。

## 常见问题

- **无法连接 MinIO**：确认 `MINIO_ENDPOINTS` 是否正确、凭证是否有效，以及是否开启 `MINIO_SECURE`。
- **图片无法展示**：检查 `IMAGE_PUBLIC_BASE` 是否能够公网/内网访问，或 Markdown 中是否使用了非图片资源。
- **搜索无结果**：确认文档后缀是否为 `.md`/`.docx`/`.doc`，并确保已执行索引同步或 Elasticsearch 运行正常。
- **DOC/DOCX 预览失败**：请确认运行环境已安装 `mammoth` 与 `textract`，其中 `textract` 需要系统依赖 `antiword`（Docker 镜像已预装）。
- **Chrome 打开 MathJax 文件后如何下载**：在打开的脚本页面中按 `Ctrl + S`（macOS 为 `Cmd + S`）或使用右上角菜单中的 **更多工具 → 保存页面为…**，将文件格式选择为“仅网页，*.js”。保存后将其上传至 `http://10.20.41.24:9005/cdn/` 对应的目录（保持原始的 `mathjax@3/es5/tex-mml-chtml.js` 路径）即可实现本地加速访问。

## License

本项目未附带许可证（No License）。如需在生产环境使用，请根据实际需求补充授权信息。
