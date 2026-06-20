import os
import re
import logging
from pathlib import Path

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
HOST = "localhost"      # 仅用于本地调试
PORT = 10000            # 同上
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
    """安全关闭多个 PIL Image 对象，自动去重，避免重复关闭抛出异常。"""
    seen = set()
    for img in imgs:
        if img is not None and id(img) not in seen:
            try:
                img.close()
            except Exception:
                pass
            seen.add(id(img))


def convert_mode(img, target_format):
    """
    根据目标格式转换色彩模式，并显式关闭所有中间图像。
    修复了 JPEG 透明转换时 split() 泄漏通道的内存泄漏。
    返回一个新的 Image 对象；调用者负责最终关闭。
    """
    fmt = target_format.upper()

    if fmt == 'JPEG':
        if img.mode == 'P':
            new_img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        else:
            new_img = img

        if new_img.mode in ('RGBA', 'LA', 'PA'):
            background = Image.new('RGB', new_img.size, (255, 255, 255))
            # 显式保存临时 convert 对象，解包全部通道
            temp_rgba = new_img.convert('RGBA')
            channels = temp_rgba.split()   # R,G,B,A
            alpha = channels[-1]
            rgb_img = new_img.convert('RGB')
            background.paste(rgb_img, mask=alpha)
            # 关闭所有临时对象
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
        # WEBP / AVIF：保留透明
        if img.mode == 'P':
            return img.convert('RGBA' if 'transparency' in img.info else 'RGB')
        elif img.mode not in ('RGB', 'RGBA'):
            return img.convert('RGBA')
        return img


def create_thumbnail(original_path, cache_path, width, height, target_format):
    """创建缩略图并写入缓存。保留 ICC 颜色配置文件以避免色彩暗淡。"""
    img = None
    converted = None
    try:
        img = Image.open(original_path)
        # 提取原始 ICC 配置文件（如果存在）
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

        # 将 ICC 注入待保存图像
        if icc_profile:
            converted.info['icc_profile'] = icc_profile

        save_kwargs = {}
        fmt = target_format.upper()
        if fmt == 'JPEG':
            save_kwargs['quality']    = 75
            save_kwargs['optimize']   = True
            save_kwargs['progressive'] = True
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
        elif fmt == 'WEBP':
            save_kwargs['quality']    = 65
            save_kwargs['method']     = 4
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
        elif fmt == 'AVIF':
            save_kwargs['quality']    = 55
            save_kwargs['speed']      = 6
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
    """处理图片请求，返回 (cache_path, error_message, status_code)"""
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
    """处理图片请求（同步方式，适配 Gunicorn 多进程模型）"""
    if '@' not in image_path or '.' not in image_path.split('@')[-1]:
        abort(400)

    # 直接同步调用，无需线程池（每个 Gunicorn worker 是独立进程，串行处理请求）
    result_path, error, status_code = process_image_request(image_path)

    if error:
        logger.warning(f"[{status_code}] {image_path}: {error}")
        abort(status_code)

    mime = format_map[image_path.split('.')[-1].lower()][1]
    return send_file(result_path, mimetype=mime)


@app.errorhandler(404)
def not_found(error):
    return "Image not found", 404


@app.errorhandler(500)
def internal_error(error):
    return "Internal server error", 500


# 本地调试入口（仅用于开发测试，生产环境由 Gunicorn 启动）
if __name__ == '__main__':
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"Starting local dev server on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)