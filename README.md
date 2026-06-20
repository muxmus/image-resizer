# image-resizer

按需图片转换与缩放服务。接收带尺寸/格式描述符的 URL 请求，自动对原图进行缩放和格式转换，结果写入磁盘缓存，后续相同请求直接命中缓存返回。

基于 **Node.js + Sharp（libvips）** 重写，替代原 Python/PIL 版本，彻底解决长时间运行后内存持续增长的问题。

---

## 目录结构

```
image-resizer/
├── app.js                # 主程序
├── ecosystem.config.js   # PM2 生产部署配置
├── package.json
├── cache-cleaner.py      # 缓存清理脚本（由 crontab 定时调用）
├── image_cache/          # 缓存目录（自动创建）
└── logs/                 # 日志目录（PM2 写入）
```

---

## 依赖

- Node.js 18+
- [Sharp](https://sharp.pixelplumbing.com/) — 底层使用 libvips，流式处理图像，内存占用极低
- [jemalloc](https://jemalloc.net/) — 替代 glibc malloc，处理完成后主动将内存归还 OS

---

## 安装

```bash
# 1. 安装 Node.js 依赖
npm install

# 2. 安装 jemalloc（解决 glibc 内存不归还 OS 的问题）
sudo apt install libjemalloc2

# 确认 .so 路径（填入下方 ecosystem.config.js）
dpkg -L libjemalloc2 | grep '\.so'
```

在 `ecosystem.config.js` 的 `env` 中填入实际路径：

```js
LD_PRELOAD: '/usr/lib/x86_64-linux-gnu/libjemalloc.so.2',
```

---

## 配置

编辑 `app.js` 顶部的常量：

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `BASE_IMG_DIR` | `/home/ubuntu/Server/img` | 原图根目录 |
| `CACHE_DIR` | `/home/ubuntu/Server/image-resizer/image_cache` | 缓存根目录 |
| `HOST` | `localhost` | 监听地址（仅供 Nginx 反代，不对外暴露） |
| `PORT` | `10000` | 监听端口 |

---

## URL 格式

请求 URL 中的文件名须包含 `@` 描述符，指定目标尺寸和格式：

```
/{子目录}/{原文件名}@{宽}w_{高}h.{格式}
/{子目录}/{原文件名}@{宽}w.{格式}
/{子目录}/{原文件名}@{高}h.{格式}
```

支持的输出格式：`webp`、`avif`、`jpg` / `jpeg`

### 示例

```
# 原图：/home/ubuntu/Server/img/photos/cat.jpg（1200×800）

/photos/cat@800w.webp       → 缩到 800×533，转 WebP
/photos/cat@600h.avif       → 缩到 900×600，转 AVIF
/photos/cat@400w_400h.jpg   → 以高为准缩到 600×400，转 JPEG（保持比例）
/photos/cat@2000w.webp      → 宽度超出原图，仅转格式不缩放，输出 1200×800
```

### 缩放规则

- **单边指定**：目标边小于原图则等比缩放，否则仅转格式
- **双边都指定**：两边都须小于原图才缩放；缩放时以**占比更大的边**为基准，保持原始比例

  ```
  原图 300×600，指定 200w_500h：
    wRatio = 200/300 = 0.667
    hRatio = 500/600 = 0.833  ← 更大
    以高为准 → 结果 250×500（而非 200×400）
  ```

- 透明通道转 JPEG 时自动合成白色背景

### 缓存机制

缓存路径与请求路径一一对应，存于 `CACHE_DIR` 下。命中判断依据原图与缓存文件的 **mtime 是否一致**（缓存写入时会同步原图 mtime），原图更新后下次请求自动重新生成。

---

## 输出质量

| 格式 | 参数 |
|------|------|
| JPEG | `quality=75`，Progressive，MozJPEG 优化 |
| WebP | `quality=65`，`effort=4` |
| AVIF | `quality=55`，`effort=4` |

所有格式均保留原图 ICC 颜色配置文件（通过 `withMetadata()`）。

---

## 运行

### 开发

```bash
node app.js
# 或热重载：
npm run dev
```

### 生产（PM2）

```bash
npm install -g pm2

# 创建日志目录
mkdir -p logs

# 启动
pm2 start ecosystem.config.js

# 开机自启
pm2 save && pm2 startup
```

常用 PM2 命令：

```bash
pm2 status                  # 查看进程状态
pm2 logs image-resizer      # 实时日志
pm2 monit                   # 内存/CPU 监控
pm2 restart image-resizer   # 重启
pm2 reload image-resizer    # 零停机热重载（cluster 模式）
```

---

## 缓存清理

`cache-cleaner.py` 负责定期扫描 `image_cache/` 并删除以下四类文件：

| 情形 | 说明 |
|------|------|
| **空文件** | Node.js 处理失败时的遗留产物；若不删除，Nginx 的 `try_files` 会命中并返回空内容，且 Node.js 永远收不到重试请求 |
| **原图已删除** | 原图不存在，对应缓存无意义 |
| **mtime 不一致** | 原图已更新，缓存已过期 |
| **长期未访问** | 超过 `CACHE_MAX_AGE`（默认 30 天）未被请求 |

### 手动执行

```bash
python3 /home/ubuntu/Server/image-resizer/cache-cleaner.py
```

### crontab 定时执行

```bash
crontab -e
```

在编辑器中添加（每天凌晨 2 点执行）：

```
0 2 * * * /usr/bin/python3 /home/ubuntu/Server/image-resizer/cache-cleaner.py >> /home/ubuntu/Server/image-resizer/logs/cache-cleaner.log 2>&1
```

查看清理日志：

```bash
tail -f /home/ubuntu/Server/image-resizer/logs/cache-cleaner.log
```

### 立即清理存量空文件

如果部署前缓存目录中已有空文件，可直接批量删除：

```bash
find /home/ubuntu/Server/image-resizer/image_cache -size 0 -delete
```

---

## Nginx 反代配置

```nginx
location / {
    root  /home/ubuntu/Server/img;
    index index.html;
}

# 匹配含 @ 描述符的图片请求（缩放/转格式）
location ~* @.*\.(avif|webp|jpg|jpeg)$ {
    root /home/ubuntu/Server/image-resizer/image_cache;
    # 缓存命中则直接由 Nginx 返回文件，未命中才转发给 Node.js
    try_files $uri @resizer_server;
}

location @resizer_server {
    proxy_pass http://localhost:10000;
    proxy_set_header Host             $host;
    proxy_set_header X-Real-IP        $remote_addr;
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 600s;
    proxy_read_timeout    600s;
    proxy_send_timeout    600s;
}
```

---

## 内存设计说明

原 Python/PIL 版本存在内存持续增长问题，根本原因有两层：

**第一层：PIL 图像对象**
PIL 将整张图完整解码到 Python 堆，处理完后依赖 GC 释放，时机不可控。  
Sharp 底层的 libvips 采用流式管道，图像分块按需解码，处理完即释放，内存占用接近恒定。

**第二层：glibc malloc 不归还内存**
libvips（以及任何 C 扩展）释放内存后，glibc 将其留在 malloc arena 的空闲链表中，不主动归还 OS。空闲多久都不会自动缩减，形成"内存水位线"。  
用 jemalloc 替换 glibc malloc 后（`LD_PRELOAD`），空闲内存会被主动归还 OS，处理完成后内存恢复至初始水位。

此外通过以下配置进一步收紧：
- `sharp.cache(false)`：禁用 libvips 操作缓存（对唯一请求无命中收益，只会堆积内存）
- `sharp.concurrency(1)`：限制 libvips 工作线程数，减少 arena 数量
