# MKViewer

轻量级的 Markdown 在线浏览器，基于 [Gradio](https://www.gradio.app/) 搭建，通过 MinIO 对象存储加载文档并进行在线预览与全文检索。适用于内部知识库、项目文档或需要快速搭建的 Markdown 阅读站点。

## 功能特性

- 📁 **目录树导航**：自动扫描 MinIO 中的 Markdown 文件，以折叠目录的方式展示，支持一键展开/折叠与刷新。
- 🔍 **全文检索**：在对象存储中对所有 Markdown 内容进行搜索，展示匹配次数与高亮片段。
- 🖼️ **图片链接重写**：将 Markdown 中的相对图片地址转换为可访问的公网/内网地址，解决跨域访问问题。
- 📝 **实时预览与源文件查看**：预览渲染后的 HTML，同时提供原始 Markdown 文本。
- 🔗 **临时下载链接**：为当前文档生成 6 小时有效的 MinIO 预签名下载链接。
- ⚡ **缓存机制**：基于 ETag 的 LRU 缓存，减少重复请求，提高加载性能。

## 环境要求

- Python ≥ 3.10（推荐使用 3.11，与 Docker 镜像保持一致）
- MinIO 或兼容的 S3 对象存储服务
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

## 配置说明

| 环境变量 | 默认值 | 说明 |
| -------- | ------ | ---- |
| `MINIO_ENDPOINTS` | `10.20.41.24:9005,10.20.40.101:9005` | MinIO 集群节点列表，多个节点用逗号分隔。 |
| `MINIO_SECURE` | `false` | 是否使用 HTTPS 连接 MinIO。 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 空 | MinIO 访问凭证。 |
| `DOC_BUCKET` | `bucket` | 存放 Markdown 文档的桶名称。 |
| `DOC_PREFIX` | 空 | 文档所在的路径前缀，可用于限定子目录。 |
| `IMAGE_PUBLIC_BASE` | `http://10.20.41.24:9005/images` | 用于重写 Markdown 图片链接的公共访问地址。 |
| `SITE_TITLE` | `通号院文档知识库` | 页面标题及顶部提示信息。 |
| `BIND_HOST` | `0.0.0.0` | 服务绑定的主机地址。 |
| `BIND_PORT` | `7861` | 服务监听端口。 |

> 其他在 `app.py` 中定义的常量也可通过环境变量覆盖。

## 使用说明

1. 打开浏览器访问部署地址，左侧为目录树与搜索栏，右侧提供文档预览/源文件/全文搜索三个标签页。
2. 选择任意 Markdown 文件后，系统会自动渲染内容，并提供临时下载链接。
3. 如需刷新目录或清理缓存，可使用左侧的控制按钮。
4. 全文搜索支持大小写不敏感匹配，返回结果按匹配次数排序。

## 开发与调试

- 代码主入口：[`app.py`](app.py)
- 主要依赖：`gradio`、`minio`、`Markdown`、`Pygments`
- 样式与 UI 控制均在 `ui_app()` 中定义，可根据需求自行扩展。
- 如需调整缓存策略，可修改 `MD_CACHE = LRU(512)` 的容量或实现。

启动开发服务器后，修改代码会在 Gradio 中自动生效；若涉及依赖更新，需重启进程。

## 常见问题

- **无法连接 MinIO**：确认 `MINIO_ENDPOINTS` 是否正确、凭证是否有效，以及是否开启 `MINIO_SECURE`。
- **图片无法展示**：检查 `IMAGE_PUBLIC_BASE` 是否能够公网/内网访问，或 Markdown 中是否使用了非图片资源。
- **搜索无结果**：系统只对 `.md`/`.markdown` 文件进行索引，请确认文档后缀。

## License

本项目未附带许可证（No License）。如需在生产环境使用，请根据实际需求补充授权信息。
