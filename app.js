/**
 * image-resizer — Node.js + Sharp 重写版
 *
 * 替代原 Python/PIL 版本，解决长时间运行内存持续增长问题。
 * Sharp 底层使用 libvips 流式管道，图像从不整体驻留内存。
 *
 * 安装：npm install
 * 开发：node app.js
 * 生产：pm2 start ecosystem.config.js
 */

'use strict';

const express              = require('express');
const sharp                = require('sharp');
const path                 = require('path');
const fsp                  = require('fs/promises');
const { execFile }         = require('child_process');
const { promisify }        = require('util');
const execFileAsync        = promisify(execFile);

// ---------- 配置（与原版保持一致） ----------
const BASE_IMG_DIR = '/home/ubuntu/Server/img';
const CACHE_DIR    = '/home/ubuntu/Server/image-resizer/image_cache';
const HOST         = 'localhost';
const PORT         = 10000;

// libvips 内部并发线程数。
// 每个线程对应一个 glibc malloc arena，线程越多保留的空闲内存越多。
// 图片处理是 I/O 密集型，单线程足够，且配合 MALLOC_ARENA_MAX=2 效果更好。
sharp.concurrency(1);

// 禁用 libvips 操作缓存。
// libvips 默认会缓存解码后的图像数据以便复用，但本服务每张图都是唯一请求，
// 缓存永远不会命中，只会无限堆积内存。这是内存持续增长的根本原因。
sharp.cache(false);

const MIME_MAP = {
  webp: 'image/webp',
  avif: 'image/avif',
  jpg:  'image/jpeg',
  jpeg: 'image/jpeg',
};

// ---------- 文件名解析 ----------
// 格式：original_name@{W}w_{H}h.ext  或  @{W}w.ext  或  @_{H}h.ext
function parseFilename(filename) {
  const m = filename.match(/^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$/);
  if (!m) return null;
  return {
    originalName: m[1],
    width:        m[3] ? parseInt(m[3], 10) : null,
    height:       m[5] ? parseInt(m[5], 10) : null,
    formatExt:    m[6].toLowerCase(),
  };
}

// ---------- 缩放逻辑（与原版 Python 完全一致） ----------

/**
 * 判断是否需要缩放。
 * 两端都指定时：两端都必须小于原图才缩放（有一端超出原图则仅转格式）。
 * 单端指定时：该端小于原图才缩放。
 */
function shouldResize(origW, origH, outW, outH) {
  if (outW !== null && outH !== null) return outW < origW && outH < origH;
  if (outW !== null)                  return outW < origW;
  if (outH !== null)                  return outH < origH;
  return false;
}

/**
 * 计算最终像素尺寸，保持原比例。
 * 两端都指定时：以占比更大的那一边为准（即 ratio 更大的边）。
 * 例：300×600 指定 200w_500h → wRatio=0.667, hRatio=0.833
 *   → hRatio 更大，以高为准 → 结果 250×500
 */
function calculateNewSize(origW, origH, outW, outH) {
  if (outW !== null && outH !== null) {
    const wRatio = outW / origW;
    const hRatio = outH / origH;
    if (wRatio >= hRatio) {
      return [outW, Math.round(outW * origH / origW)];
    } else {
      return [Math.round(outH * origW / origH), outH];
    }
  } else if (outW !== null) {
    return [outW, Math.round(outW * origH / origW)];
  } else {
    return [Math.round(outH * origW / origH), outH];
  }
}

// ---------- 核心处理 ----------
async function processImage(imagePath) {
  const filename = path.basename(imagePath);
  const parsed   = parseFilename(filename);

  if (!parsed)                          return { status: 400, error: 'Invalid filename format' };
  if (!(parsed.formatExt in MIME_MAP))  return { status: 415, error: `Unsupported format: ${parsed.formatExt}` };

  const { originalName, width, height, formatExt } = parsed;
  const dir       = path.dirname(imagePath);
  const origPath  = path.join(BASE_IMG_DIR, dir.replace(/^\//, ''), originalName);
  const cachePath = path.join(CACHE_DIR,    imagePath.replace(/^\//, ''));
  const mime      = MIME_MAP[formatExt];

  // 原图存在？
  let origStat;
  try {
    origStat = await fsp.stat(origPath);
  } catch {
    return { status: 404, error: 'Original image not found' };
  }

  // 缓存命中：mtime 精确相等（touch -r 保证纳秒对齐，Node.js stat 读回为 ms，两侧一致）
  try {
    const cacheStat = await fsp.stat(cachePath);
    if (cacheStat.size === 0) {
      // 上次处理失败遗留的空文件，删除后重新生成
      await fsp.unlink(cachePath);
    } else if (origStat.mtimeMs === cacheStat.mtimeMs) {
      return { status: 200, cachePath, mime };
    }
  } catch {
    // 缓存不存在，继续生成
  }

  // 读取原图尺寸（用于精确计算缩放比）
  let origW, origH;
  try {
    const meta = await sharp(origPath, { limitInputPixels: 80_000_000 }).metadata();
    origW = meta.width;
    origH = meta.height;
  } catch (err) {
    console.error(`[ERR] metadata ${origPath}:`, err.message);
    return { status: 500, error: 'Failed to read image metadata' };
  }

  // 按原版逻辑计算最终尺寸
  let finalW = null, finalH = null, doResize = false;
  if (width !== null || height !== null) {
    doResize = shouldResize(origW, origH, width, height);
    if (doResize) {
      [finalW, finalH] = calculateNewSize(origW, origH, width, height);
    }
  }

  // 生成缓存
  // 策略：先写入 .tmp 临时文件，成功后再原子 rename 到最终路径。
  // 若进程在写入过程中被 PM2 强杀（OOM 等），.tmp 文件会残留，
  // 但 Nginx 的 try_files 匹配 "@.*\.(avif|webp|...)" 不会命中 .tmp，
  // 不会向客户端返回残损内容，下次请求可正常重新生成。
  const tmpPath = cachePath + '.tmp';
  try {
    await fsp.mkdir(path.dirname(cachePath), { recursive: true });

    let pipeline = sharp(origPath, {
      limitInputPixels: 80_000_000,
      sequentialRead:   true,
    });

    if (doResize) {
      pipeline = pipeline.resize(finalW, finalH, { fit: 'fill' });
    }

    switch (formatExt) {
      case 'jpg':
      case 'jpeg':
        pipeline = pipeline
          .flatten({ background: '#ffffff' })
          .jpeg({ quality: 75, progressive: true, mozjpeg: true });
        break;
      case 'webp':
        pipeline = pipeline.webp({ quality: 65, effort: 4 });
        break;
      case 'avif':
        // effort: 0 = 最快模式，速度比 effort:4 快 3-5 倍，
        // 对 4K 图像尤为明显；文件体积略增但质量仍可接受。
        pipeline = pipeline.avif({ quality: 55, effort: 0 });
        break;
    }

    // 写入临时文件
    await pipeline.withMetadata().toFile(tmpPath);

    // touch -r 以纳秒精度把原图 mtime 复制到临时文件，
    // 确保 rename 后与 cache-cleaner.py 的 st_mtime 比较完全一致。
    await execFileAsync('touch', ['-r', origPath, tmpPath]);

    // 原子重命名（同一文件系统上 rename 是原子操作）
    await fsp.rename(tmpPath, cachePath);

    const sizeInfo = doResize ? `${finalW}x${finalH}` : `${origW}x${origH} (no resize)`;
    console.info(`[OK]  ${cachePath} → ${sizeInfo}`);
    return { status: 200, cachePath, mime };

  } catch (err) {
    console.error(`[ERR] ${cachePath}:`, err.message);
    // 清理临时文件（正常抛出异常时走这里；进程被强杀则由 cache-cleaner 清理）
    try { await fsp.unlink(tmpPath); } catch { /* ignore */ }
    return { status: 500, error: 'Failed to create thumbnail' };
  }
}

// ---------- Express 路由 ----------
const app = express();

app.get(/.*/, async (req, res) => {
  // req.path 保留原始百分号编码，必须手动解码才能匹配磁盘上的中文文件名
  let imagePath;
  try {
    imagePath = decodeURIComponent(req.path);
  } catch {
    return res.status(400).send('Bad request');
  }

  // 快速过滤：必须含 @ 符号
  if (!imagePath.includes('@')) {
    return res.status(400).send('Bad request');
  }

  const { status, cachePath, mime, error } = await processImage(imagePath);

  if (error) {
    console.warn(`[${status}] ${imagePath}: ${error}`);
    return res.status(status).send(error);
  }

  res.setHeader('Content-Type', mime);
  // sendFile 要求绝对路径；cachePath 已是绝对路径
  res.sendFile(cachePath);
});

// ---------- 启动 ----------
(async () => {
  await fsp.mkdir(CACHE_DIR, { recursive: true });
  app.listen(PORT, HOST, () => {
    console.log(`Image resizer running at http://${HOST}:${PORT}`);
  });
})();
