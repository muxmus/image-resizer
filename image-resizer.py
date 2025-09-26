import os
import re
import time
import threading
from PIL import Image
from flask import Flask, send_file, abort, request
from pathlib import Path
from datetime import datetime, timedelta
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

#   格式参考B站，示例：
#   http://127.0.0.1:10000/example.jpg@1920w_1080h.webp
#   http://127.0.0.1:10000/example.png@800w_800h.avif
#   http://127.0.0.1:10000/example.webp@512h.jpg
#   http://127.0.0.1:10000/example.avif@256w.png
#   http://127.0.0.1:10000/example.ico@.gif
#   http://127.0.0.1:10000/example.gif@64w_64h.ico
#   宽或高超出原图时，仅转换格式，不改变图片尺寸
#   宽和高都指定时，依旧保持原比例，以在原宽或原高中占比更大的那一边为标准进行缩小

# 配置
BASE_IMG_DIR = "/path/to/img"
CACHE_DIR = "/path/to/image-resizer/image_cache"
HOST = "localhost"
PORT = 10000
MAX_WORKERS = 4  # 最大并发处理数

# 支持的格式映射
format_map = {
    'webp': ('WEBP', 'image/webp'),
    'avif': ('AVIF', 'image/avif'),
    'jpg': ('JPEG', 'image/jpeg'),
    'jpeg': ('JPEG', 'image/jpeg'),
    'png': ('PNG', 'image/png'),
    'gif': ('GIF', 'image/gif'),
    'ico': ('ICO', 'image/x-icon')
}

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
    格式: original.jpg@{width}w_{height}h.{format}
    """
    pattern = r'^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$'
    match = re.match(pattern, filename)
    
    if not match:
        return None, None, None, None
    
    original_name = match.group(1)
    width_str = match.group(3)
    height_str = match.group(5)
    format_ext = match.group(6)
    
    width = int(width_str) if width_str else None
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
        # 同时指定宽高
        return out_w < orig_width and out_h < orig_height
    elif out_w is not None:
        # 只指定宽度
        return out_w < orig_width
    elif out_h is not None:
        # 只指定高度
        return out_h < orig_height
    else:
        # 没有指定尺寸，只转换格式
        return False

def calculate_new_size(orig_width, orig_height, out_w, out_h):
    """计算新的尺寸"""
    if out_w is not None and out_h is not None:
        # 计算宽高比
        width_ratio = out_w / orig_width
        height_ratio = out_h / orig_height
        
        if width_ratio >= height_ratio:
            out_h = round(out_w * orig_height / orig_width)
        else:
            out_w = round(out_h * orig_width / orig_height)
            
    elif out_w is not None:
        # 只指定宽度
        out_h = round(out_w * orig_height / orig_width)
        
    elif out_h is not None:
        # 只指定高度
        out_w = round(out_h * orig_width / orig_height)
        
    else:
        # 不调整大小
        out_w, out_h = orig_width, orig_height
        
    return out_w, out_h

def create_thumbnail(original_path, cache_path, width, height, target_format):
    """创建缩略图"""
    try:
        with Image.open(original_path) as img:
            # 转换模式
            if img.mode not in ('RGB', 'RGBA'):
                if img.mode == 'P':
                    img = img.convert('RGBA' if img.info.get('transparency') else 'RGB')
                else:
                    img = img.convert('RGB')
            
            orig_width, orig_height = img.size
            
            # 检查是否需要调整大小
            if should_resize(orig_width, orig_height, width, height):
                new_width, new_height = calculate_new_size(orig_width, orig_height, width, height)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            else:
                new_width, new_height = orig_width, orig_height
            
            # 确保输出目录存在
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            
            # 保存图片
            save_kwargs = {}
            if target_format.upper() == 'JPEG':
                save_kwargs['quality'] = 85
                save_kwargs['optimize'] = True
            elif target_format.upper() == 'WEBP':
                save_kwargs['quality'] = 80
                save_kwargs['method'] = 6
            
            img.save(cache_path, format=target_format, **save_kwargs)
            
            # 设置缓存文件的mtime为原始文件的mtime
            orig_mtime = os.path.getmtime(original_path)
            os.utime(cache_path, (orig_mtime, orig_mtime))
            
            logger.info(f"Created thumbnail: {cache_path} ({new_width}x{new_height})")
            return True
            
    except Exception as e:
        logger.error(f"Error creating thumbnail {cache_path}: {str(e)}")
        return False

def process_image_request(relative_path):
    """处理图片请求"""
    filename = os.path.basename(relative_path)
    original_name, width, height, format_ext = parse_filename(filename)
    
    if not original_name:
        return None, "Invalid filename format"
    
    if format_ext not in format_map:
        return None, f"Unsupported format: {format_ext}"
    
    target_format, mime_type = format_map[format_ext]
    
    # 构建路径
    original_relative = os.path.join(os.path.dirname(relative_path), original_name)
    original_path = get_original_image_path(original_relative)
    cache_path = get_cache_image_path(relative_path)
    
    # 检查原始文件是否存在
    if not os.path.exists(original_path):
        return None, "Original image not found"
    
    # 检查缓存文件是否存在且mtime一致
    if os.path.exists(cache_path):
        orig_mtime = os.path.getmtime(original_path)
        cache_mtime = os.path.getmtime(cache_path)
        
        if orig_mtime == cache_mtime:
            return cache_path, None
    
    # 创建缩略图
    if create_thumbnail(original_path, cache_path, width, height, target_format):
        return cache_path, None
    else:
        return None, "Failed to create thumbnail"

@app.route('/<path:image_path>')
def serve_image(image_path):
    """处理图片请求"""
    # 检查是否是需要处理的格式
    if '@' not in image_path or '.' not in image_path.split('@')[-1]:
        # 直接访问原图，返回404，由nginx处理
        abort(404)
    
    # 使用线程池处理请求，限制并发数
    future = executor.submit(process_image_request, image_path)
    
    with task_lock:
        processing_tasks[image_path] = future
    
    try:
        result_path, error = future.result(timeout=30)  # 30秒超时
        
        with task_lock:
            if image_path in processing_tasks:
                del processing_tasks[image_path]
        
        if error:
            logger.warning(f"Image processing failed for {image_path}: {error}")
            abort(404)
        
        # 返回文件
        return send_file(result_path, mimetype=format_map[image_path.split('.')[-1]][1])
        
    except Exception as e:
        with task_lock:
            if image_path in processing_tasks:
                del processing_tasks[image_path]
        
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
    # 确保缓存目录存在
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    logger.info(f"Starting image resizer on {HOST}:{PORT}")
    logger.info(f"Base image directory: {BASE_IMG_DIR}")
    logger.info(f"Cache directory: {CACHE_DIR}")
    logger.info(f"Max workers: {MAX_WORKERS}")
    
    app.run(host=HOST, port=PORT, threaded=True)

