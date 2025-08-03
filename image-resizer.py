from flask import Flask, send_file, make_response
from PIL import Image
import pillow_avif
from io import BytesIO
import time
import os
import threading
import functools
import re
import urllib.parse
import hashlib
import sys
import shutil
import atexit

# 设置工作目录为脚本所在目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
app.config['MAX_CONCURRENT_PROCESSING'] = 8
app.config['CACHE_TTL'] = 60 * 60 * 24  # 24小时
app.config['MEMORY_CACHE_SIZE'] = 200  # 内存缓存项数量
app.config['DISK_CACHE_DIR'] = './image_cache'  # 磁盘缓存目录
app.config['MAX_DISK_CACHE_SIZE'] = 2 * 1024 * 1024 * 1024  # 2GB磁盘缓存
app.config['SMALL_FILE_THRESHOLD'] = 1024 * 500  # 500KB以下文件保留内存缓存

# 创建缓存目录
if not os.path.exists(app.config['DISK_CACHE_DIR']):
    os.makedirs(app.config['DISK_CACHE_DIR'])

# 内存缓存字典（仅用于小文件）
image_cache = {}
cache_lock = threading.Lock()

# 处理信号量限制并发
processing_semaphore = threading.Semaphore(app.config['MAX_CONCURRENT_PROCESSING'])

# 缓存清理线程
cleanup_running = True
def cache_cleanup_thread():
    """定期清理过期缓存"""
    while cleanup_running:
        time.sleep(60 * 60)  # 每小时清理一次
        try:
            now = time.time()
            cache_dir = app.config['DISK_CACHE_DIR']
            total_size = 0
            
            # 计算目录大小并删除过期文件
            for filename in os.listdir(cache_dir):
                filepath = os.path.join(cache_dir, filename)
                try:
                    stat = os.stat(filepath)
                    # 删除过期文件
                    if now - stat.st_mtime > app.config['CACHE_TTL']:
                        os.remove(filepath)
                    else:
                        total_size += stat.st_size
                except Exception:
                    continue
            
            # 清理超过大小限制的旧文件
            if total_size > app.config['MAX_DISK_CACHE_SIZE']:
                files = []
                for filename in os.listdir(cache_dir):
                    filepath = os.path.join(cache_dir, filename)
                    try:
                        stat = os.stat(filepath)
                        files.append((filepath, stat.st_mtime))
                    except Exception:
                        continue
                
                # 按修改时间排序（旧文件在前）
                files.sort(key=lambda x: x[1])
                while total_size > app.config['MAX_DISK_CACHE_SIZE'] and files:
                    filepath, _ = files.pop(0)
                    try:
                        file_size = os.path.getsize(filepath)
                        os.remove(filepath)
                        total_size -= file_size
                    except Exception:
                        pass
        except Exception as e:
            print(f"Cache cleanup error: {e}")

# 启动清理线程
cleanup_thread = threading.Thread(target=cache_cleanup_thread, daemon=True)
cleanup_thread.start()

# 退出时停止清理线程
def stop_cleanup():
    global cleanup_running
    cleanup_running = False
atexit.register(stop_cleanup)

def synchronized(lock):
    """线程安全装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)
        return wrapper
    return decorator

def get_cache_path(key):
    """获取缓存文件路径"""
    return os.path.join(app.config['DISK_CACHE_DIR'], key)

@synchronized(cache_lock)
def get_cached_image(key):
    """获取缓存（优先内存，其次磁盘）"""
    # 检查内存缓存
    if key in image_cache:
        cached_time, data = image_cache[key]
        if time.time() - cached_time < app.config['CACHE_TTL']:
            return data
    
    # 检查磁盘缓存
    cache_path = get_cache_path(key)
    if os.path.exists(cache_path):
        file_time = os.path.getmtime(cache_path)
        if time.time() - file_time < app.config['CACHE_TTL']:
            try:
                with open(cache_path, 'rb') as f:
                    data = f.read()
                
                # 小文件保留在内存中
                if len(data) <= app.config['SMALL_FILE_THRESHOLD']:
                    image_cache[key] = (time.time(), data)
                
                return data
            except Exception:
                pass
    return None

@synchronized(cache_lock)
def set_cached_image(key, data):
    """设置缓存（磁盘+小文件内存缓存）"""
    # 写入磁盘
    cache_path = get_cache_path(key)
    try:
        temp_path = cache_path + '.tmp'
        with open(temp_path, 'wb') as f:
            f.write(data)
        os.replace(temp_path, cache_path)  # 原子操作
    except Exception as e:
        print(f"Failed to write cache: {e}")
        try:
            os.remove(temp_path)
        except:
            pass
        return
    
    # 小文件保留内存缓存
    if len(data) <= app.config['SMALL_FILE_THRESHOLD']:
        image_cache[key] = (time.time(), data)
    
    # 清理内存缓存
    if len(image_cache) > app.config['MEMORY_CACHE_SIZE']:
        oldest_key = min(image_cache, key=lambda k: image_cache[k][0])
        del image_cache[oldest_key]

def safe_path(path):
    """安全处理路径，防止目录遍历攻击并支持中文"""
    try:
        # 解码URL编码的路径
        decoded_path = urllib.parse.unquote(path)
        
        # 移除潜在的目录遍历攻击
        clean_path = re.sub(r'(\.\./|\.\\)', '', decoded_path)
        
        # 允许中文字符、字母、数字、基本符号
        if not re.match(r'^[\w\-\/@.\u4e00-\u9fff]+$', clean_path):
            return None
        
        return clean_path
    except Exception as e:
        return None

@app.route('/<path:path>', methods=['GET'])
def resizer(path):
    # 安全处理路径
    clean_path = safe_path(path)
    if clean_path is None:
        return make_response('Invalid path', 400)
    
    try:
        # 解析路径参数
        parts = clean_path.split('@')
        if len(parts) < 2:
            return make_response('Invalid format', 400)
            
        # 安全构建原始文件路径
        base_dir = os.path.abspath('/path/to/img/')
        orig_file = os.path.abspath(os.path.join(
            base_dir, 
            parts[0].lstrip('/')
        ))
        
        # 验证原始文件路径是否在允许的目录内
        if not orig_file.startswith(base_dir):
            return make_response('Invalid path', 400)
        
        # 继续处理剩余部分
        params = parts[1].split('.')
        if len(params) < 2:
            return make_response('Invalid parameters', 400)
            
        file_ext = params[1].lower()
        size_param = params[0]
        
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
        
        if file_ext not in format_map:
            return make_response('Unsupported format', 400)
            
        io_type, io_mimetype = format_map[file_ext]
        
        # 创建安全的ASCII缓存键（避免中文问题）
        cache_key = hashlib.md5(clean_path.encode('utf-8')).hexdigest() + f"_{io_type}"
        
        # 尝试从缓存获取
        cached_data = get_cached_image(cache_key)
        if cached_data:
            # 使用ASCII安全的ETag
            return send_file(
                BytesIO(cached_data),
                mimetype=io_mimetype,
                etag=cache_key
            )
            
        # 检查原始文件是否存在
        if not os.path.isfile(orig_file):
            return make_response('Original image not found', 404)
            
        # 使用信号量控制并发处理
        with processing_semaphore:
            # 再次检查缓存（可能在等待期间已被其他线程生成）
            cached_data = get_cached_image(cache_key)
            if cached_data:
                return send_file(
                    BytesIO(cached_data),
                    mimetype=io_mimetype,
                    etag=cache_key
                )
                
            # 处理图片
            with Image.open(orig_file) as img:
                # 获取原始尺寸
                orig_width, orig_height = img.size
                
                # 处理尺寸参数
                if 'w' in size_param and 'h' in size_param:
                    width_part, height_part = size_param.split('_')
                    out_w = int(width_part.replace('w', ''))
                    out_h = int(height_part.replace('h', ''))
                    
                    # 计算宽高比
                    width_ratio = out_w / orig_width
                    height_ratio = out_h / orig_height
                    
                    # 只有当目标尺寸小于原图时才调整大小
                    if width_ratio < 1 or height_ratio < 1:
                        if width_ratio >= height_ratio:
                            out_h = round(out_w * orig_height / orig_width)
                        else:
                            out_w = round(out_h * orig_width / orig_height)
                        
                        img = img.resize((out_w, out_h), Image.LANCZOS)
                
                elif 'w' in size_param:
                    out_w = int(size_param.replace('w', ''))
                    if out_w < orig_width:
                        out_h = round(out_w * orig_height / orig_width)
                        img = img.resize((out_w, out_h), Image.LANCZOS)
                
                elif 'h' in size_param:
                    out_h = int(size_param.replace('h', ''))
                    if out_h < orig_height:
                        out_w = round(out_h * orig_width / orig_height)
                        img = img.resize((out_w, out_h), Image.LANCZOS)
                
                # 转换格式
                if io_type == 'JPEG':
                    img = img.convert('RGB')
                
                # 保存到内存
                img_bytes = BytesIO()
                
                # 根据格式优化保存参数
                save_args = {'format': io_type, 'optimize': True}
                if io_type == 'JPEG':
                    save_args['quality'] = 85
                elif io_type == 'WEBP':
                    save_args['quality'] = 80
                elif io_type == 'AVIF':
                    save_args['quality'] = 75
                
                img.save(img_bytes, **save_args)
                img_data = img_bytes.getvalue()
                
                # 更新缓存
                set_cached_image(cache_key, img_data)
                
                # 返回响应
                return send_file(
                    BytesIO(img_data),
                    mimetype=io_mimetype,
                    etag=cache_key
                )
                
    except Exception as e:
        return make_response('Internal server error', 500)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=10000, threaded=True)
