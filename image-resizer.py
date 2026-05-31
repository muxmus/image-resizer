import os
import re
import time
import threading
from PIL import Image
try:
    import pillow_avif  # 注册 AVIF 编解码器（pip install pillow-avif-plugin）
    AVIF_SUPPORTED = True
except ImportError:
    AVIF_SUPPORTED = False
from flask import Flask, send_file, abort, request
from pathlib import Path
from datetime import datetime, timedelta
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# 格式参考B站，示例：
# http://127.0.0.1:10000/example.jpg@1920w_1080h.webp
# http://127.0.0.1:10000/example.png@800w_800h.avif
# http://127.0.0.1:10000/example.webp@512h.jpg
# http://127.0.0.1:10000/example.avif@256w.png
# http://127.0.0.1:10000/example.avif@256w.webp
# http://127.0.0.1:10000/example.webp@800w.avif
#
# 输入支持：jpg/jpeg、png、webp、avif（需 Pillow >= 9.1 且系统有 libavif）
# 输出支持：jpg、webp、avif
#
# 宽或高超出原图时，仅转换格式，不改变图片尺寸
# 宽和高都指定时，依旧保持原比例，以在原宽或原高中占比更大的那一边为标准进行缩小

# 配置
BASE_IMG_DIR = "/path/to/img"
CACHE_DIR = "/path/to/image-resizer/image_cache"
HOST = "localhost"
PORT = 10000
MAX_WORKERS = 4  # 最大并发处理数

# 支持的输出格式映射
format_map = {
    'webp': ('WEBP', 'image/webp'),
    'avif': ('AVIF', 'image/avif'),
    'jpg':  ('JPEG', 'image/jpeg'),
    'jpeg': ('JPEG', 'image/jpeg'),
}

# 支持的输入扩展名（用于注释说明，实际由 Pillow 决定能否打开）
SUPPORTED_INPUT_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.avif'}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 线程池执行器，限制并发数
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
processing_tasks = {}
task_lock = threading.Lock()


def parse_filename(filename):
    """
    解析文件名，提取原始文件名、宽度、高度和格式
    格式: original.ext@{width}w_{height}h.{output_format}
    """
    pattern = r'^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$'
    match = re.match(pattern, filename)
    if not match:
        return None, None, None, None

    original_name = match.group(1)
    width_str    = match.group(3)
    height_str   = match.group(5)
    format_ext   = match.group(6).lower()

    width  = int(width_str)  if width_str  else None
    height = int(height_str) if height_str else None

    return original_name, width, height, format_ext


def get_original_image_path(relative_path):
    """获取原始图片路径"""
    return os.path.join(BASE_IMG_DIR, relative_path.lstrip('/'))


def get_cache_image_path(relative_path):
    """获取缓存图片路径"""
    return os.path.join(CACHE_DIR, relative_path.lstrip('/'))


def should_resize(orig_width, orig_height, out_w, out_h):
    """判断是否需要调整大小"""
    if out_w is not None and out_h is not None:
        return out_w < orig_width and out_h < orig_height
    elif out_w is not None:
        return out_w < orig_width
    elif out_h is not None:
        return out_h < orig_height
    else:
        return False


def calculate_new_size(orig_width, orig_height, out_w, out_h):
    """计算新的尺寸（保持原比例）"""
    if out_w is not None and out_h is not None:
        width_ratio  = out_w / orig_width
        height_ratio = out_h / orig_height
        if width_ratio >= height_ratio:
            out_h = round(out_w * orig_height / orig_width)
        else:
            out_w = round(out_h * orig_width / orig_height)
    elif out_w is not None:
        out_h = round(out_w * orig_height / orig_width)
    elif out_h is not None:
        out_w = round(out_h * orig_width / orig_height)
    else:
        out_w, out_h = orig_width, orig_height
    return out_w, out_h


def convert_mode(img, target_format):
    """
    根据目标格式转换图像色彩模式。
    - JPEG 不支持透明通道，RGBA/PA 需合并到白色背景
    - AVIF / WEBP 原生支持 RGBA，保留透明
    """
    fmt = target_format.upper()

    if fmt == 'JPEG':
        # JPEG 必须是 RGB，无透明
        if img.mode == 'P':
            img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        if img.mode in ('RGBA', 'LA', 'PA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            alpha = img.convert('RGBA').split()[-1]
            background.paste(img.convert('RGB'), mask=alpha)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
    else:
        # WEBP / AVIF：保留透明通道
        if img.mode == 'P':
            img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        elif img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')

    return img


def create_thumbnail(original_path, cache_path, width, height, target_format):
    """创建缩略图并写入缓存"""
    try:
        with Image.open(original_path) as img:
            img = convert_mode(img, target_format)

            orig_width, orig_height = img.size

            if should_resize(orig_width, orig_height, width, height):
                new_width, new_height = calculate_new_size(orig_width, orig_height, width, height)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            else:
                new_width, new_height = orig_width, orig_height

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)

            save_kwargs = {}
            fmt = target_format.upper()

            if fmt == 'JPEG':
                save_kwargs['quality']   = 85
                save_kwargs['optimize']  = True

            elif fmt == 'WEBP':
                save_kwargs['quality']   = 80
                save_kwargs['method']    = 6       # 压缩努力度 0-6，6 最慢但最小

            elif fmt == 'AVIF':
                save_kwargs['quality']   = 80      # 0-100，越高越好；Pillow >= 9.1
                save_kwargs['speed']     = 6       # 0-10，越高越快（质量略低）

            img.save(cache_path, format=target_format, **save_kwargs)

            # 缓存文件 mtime 与原始文件保持一致，用于缓存有效性判断
            orig_mtime = os.path.getmtime(original_path)
            os.utime(cache_path, (orig_mtime, orig_mtime))

            logger.info(f"Created: {cache_path} ({new_width}x{new_height}, {target_format})")
            return True

    except Exception as e:
        logger.error(f"Error creating thumbnail {cache_path}: {str(e)}")
        return False


def process_image_request(relative_path):
    """处理图片请求，返回 (cache_path, error_message, status_code)"""
    filename = os.path.basename(relative_path)
    original_name, width, height, format_ext = parse_filename(filename)

    if not original_name:
        return None, "Invalid filename format", 400

    if format_ext not in format_map:
        return None, f"Unsupported output format: {format_ext}", 415

    target_format, mime_type = format_map[format_ext]

    # 构建路径
    original_relative = os.path.join(os.path.dirname(relative_path), original_name)
    original_path = get_original_image_path(original_relative)
    cache_path    = get_cache_image_path(relative_path)

    if not os.path.exists(original_path):
        return None, "Original image not found", 404

    # 检查缓存是否仍有效（mtime 一致）
    if os.path.exists(cache_path):
        orig_mtime  = os.path.getmtime(original_path)
        cache_mtime = os.path.getmtime(cache_path)
        if orig_mtime == cache_mtime:
            return cache_path, None, 200

    if create_thumbnail(original_path, cache_path, width, height, target_format):
        return cache_path, None, 200
    else:
        return None, "Failed to create thumbnail", 500


@app.route('/<path:image_path>')
def serve_image(image_path):
    """处理图片请求"""
    if '@' not in image_path or '.' not in image_path.split('@')[-1]:
        abort(400)

    future = executor.submit(process_image_request, image_path)
    with task_lock:
        processing_tasks[image_path] = future

    try:
        result_path, error, status_code = future.result(timeout=30)
        with task_lock:
            processing_tasks.pop(image_path, None)

        if error:
            logger.warning(f"[{status_code}] {image_path}: {error}")
            abort(status_code)

        mime = format_map[image_path.split('.')[-1].lower()][1]
        return send_file(result_path, mimetype=mime)

    except Exception as e:
        from werkzeug.exceptions import HTTPException
        with task_lock:
            processing_tasks.pop(image_path, None)
        if isinstance(e, HTTPException):
            raise
        logger.error(f"Error processing {image_path}: {str(e)}")
        abort(500)


@app.errorhandler(404)
def not_found(error):
    return "Image not found", 404


@app.errorhandler(500)
def internal_error(error):
    return "Internal server error", 500


def get_processing_count():
    """获取当前正在处理的任务数量"""
    with task_lock:
        return len([f for f in processing_tasks.values() if not f.done()])


if __name__ == '__main__':
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"Starting image resizer on {HOST}:{PORT}")
    logger.info(f"Base image directory: {BASE_IMG_DIR}")
    logger.info(f"Cache directory: {CACHE_DIR}")
    logger.info(f"Max workers: {MAX_WORKERS}")
    if AVIF_SUPPORTED:
        logger.info("AVIF support: enabled (pillow-avif-plugin)")
    else:
        logger.warning("AVIF support: DISABLED — run: pip install pillow-avif-plugin")
    app.run(host=HOST, port=PORT, threaded=True)
