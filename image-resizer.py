import os
import re
import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from waitress import serve
from flask import Flask, send_file, abort, request
from PIL import Image

try:
    import pillow_avif
    AVIF_SUPPORTED = True
except ImportError:
    AVIF_SUPPORTED = False

# ---------- 配置 ----------
BASE_IMG_DIR = "/home/ubuntu/Server/img"
CACHE_DIR = "/home/ubuntu/Server/image-resizer/image_cache"
HOST = "localhost"
PORT = 10000
MAX_WORKERS = 4
MAX_IMAGE_PIXELS = 80_000_000

format_map = {
    'webp': ('WEBP', 'image/webp'),
    'avif': ('AVIF', 'image/avif'),
    'jpg':  ('JPEG', 'image/jpeg'),
    'jpeg': ('JPEG', 'image/jpeg'),
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
processing_tasks = {}
task_lock = threading.Lock()


def shutdown_executor():
    logger.info("Shutting down executor...")
    executor.shutdown(wait=True)
atexit.register(shutdown_executor)


# ---------- 工具函数 ----------
def parse_filename(filename):
    pattern = r'^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$'
    match = re.match(pattern, filename)
    if not match:
        return None, None, None, None
    original_name = match.group(1)
    width_str     = match.group(3)
    height_str    = match.group(5)
    format_ext    = match.group(6).lower()
    width  = int(width_str)  if width_str  else None
    height = int(height_str) if height_str else None
    return original_name, width, height, format_ext


def get_original_image_path(relative_path):
    return os.path.join(BASE_IMG_DIR, relative_path.lstrip('/'))


def get_cache_image_path(relative_path):
    return os.path.join(CACHE_DIR, relative_path.lstrip('/'))


def should_resize(orig_width, orig_height, out_w, out_h):
    if out_w is not None and out_h is not None:
        return out_w < orig_width and out_h < orig_height
    elif out_w is not None:
        return out_w < orig_width
    elif out_h is not None:
        return out_h < orig_height
    else:
        return False


def calculate_new_size(orig_width, orig_height, out_w, out_h):
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


def _close_and_del(*imgs):
    seen = set()
    for img in imgs:
        if img is not None and id(img) not in seen:
            try:
                img.close()
            except Exception:
                pass
            seen.add(id(img))


def convert_mode(img, target_format):
    fmt = target_format.upper()

    if fmt == 'JPEG':
        if img.mode == 'P':
            new_img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        else:
            new_img = img

        if new_img.mode in ('RGBA', 'LA', 'PA'):
            background = Image.new('RGB', new_img.size, (255, 255, 255))
            temp_rgba = new_img.convert('RGBA')
            channels = temp_rgba.split()
            alpha = channels[-1]
            rgb_img = new_img.convert('RGB')
            background.paste(rgb_img, mask=alpha)
            _close_and_del(temp_rgba, *channels, rgb_img)
            if new_img is not img:
                _close_and_del(new_img)
            return background
        else:
            if new_img.mode != 'RGB':
                final = new_img.convert('RGB')
                if new_img is not img:
                    _close_and_del(new_img)
                return final
            if new_img is not img:
                return new_img
            return img
    else:
        if img.mode == 'P':
            return img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        elif img.mode not in ('RGB', 'RGBA'):
            return img.convert('RGBA')
        return img


def create_thumbnail(original_path, cache_path, width, height, target_format):
    """创建缩略图并写入缓存，保留 ICC 颜色配置文件以修正 WebP/JPEG 色彩暗淡问题。"""
    img = None
    converted = None
    try:
        img = Image.open(original_path)
        # ✅ 提取原始 ICC 配置文件（如果存在）
        icc_profile = img.info.get('icc_profile')

        if img.width * img.height > MAX_IMAGE_PIXELS:
            logger.warning(f"Image too large: {original_path} ({img.width}x{img.height})")
            return False

        converted = convert_mode(img, target_format)
        orig_width, orig_height = converted.size

        if should_resize(orig_width, orig_height, width, height):
            new_width, new_height = calculate_new_size(orig_width, orig_height, width, height)
            resized = converted.resize((new_width, new_height), Image.Resampling.LANCZOS)
            _close_and_del(converted)
            converted = resized
        else:
            new_width, new_height = orig_width, orig_height

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        # ✅ 如果有 ICC 配置，将其注入到待保存的图像对象
        if icc_profile:
            converted.info['icc_profile'] = icc_profile

        save_kwargs = {}
        fmt = target_format.upper()
        if fmt == 'JPEG':
            save_kwargs['quality']    = 75
            save_kwargs['optimize']   = True
            save_kwargs['progressive'] = True
            # JPEG 支持显式传递 ICC 配置文件
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
        elif fmt == 'WEBP':
            save_kwargs['quality']    = 65
            save_kwargs['method']     = 4
            # WebP 同样支持 icc_profile 参数
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
        elif fmt == 'AVIF':
            save_kwargs['quality']    = 55
            save_kwargs['speed']      = 6
            # pillow_avif 会自动从 info 读取 icc_profile，但也可以显式传递
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile

        converted.save(cache_path, format=target_format, **save_kwargs)

        orig_mtime = os.path.getmtime(original_path)
        os.utime(cache_path, (orig_mtime, orig_mtime))

        logger.info(f"Created: {cache_path} ({new_width}x{new_height}, {target_format})")
        return True

    except Exception as e:
        logger.error(f"Error creating thumbnail {cache_path}: {str(e)}")
        return False
    finally:
        _close_and_del(img, converted)


def process_image_request(relative_path):
    filename = os.path.basename(relative_path)
    original_name, width, height, format_ext = parse_filename(filename)
    if not original_name:
        return None, "Invalid filename format", 400
    if format_ext not in format_map:
        return None, f"Unsupported output format: {format_ext}", 415

    target_format, mime_type = format_map[format_ext]
    original_relative = os.path.join(os.path.dirname(relative_path), original_name)
    original_path = get_original_image_path(original_relative)
    cache_path    = get_cache_image_path(relative_path)

    if not os.path.exists(original_path):
        return None, "Original image not found", 404

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
    with task_lock:
        return len([f for f in processing_tasks.values() if not f.done()])


if __name__ == '__main__':
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"Starting image resizer on {HOST}:{PORT}")
    serve(app, host=HOST, port=PORT, threads=8)
