import os
import time
from pathlib import Path
from datetime import datetime, timedelta
import logging

# 配置
BASE_IMG_DIR = "/path/to/img"
CACHE_DIR = "/path/to/image-resizer/image_cache"
CACHE_MAX_AGE = 30  # 缓存最大天数

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def parse_filename(filename):
    """解析缓存文件名，提取原始文件名"""
    import re
    pattern = r'^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$'
    match = re.match(pattern, filename)
    return match.group(1) if match else None

def cleanup_cache():
    """清理缓存"""
    logger.info("Starting cache cleanup...")
    
    cache_files_checked = 0
    cache_files_deleted = 0
    errors = 0
    
    for cache_file_path in Path(CACHE_DIR).rglob('*@*.*'):
        try:
            cache_files_checked += 1
            relative_path = cache_file_path.relative_to(CACHE_DIR)
            filename = relative_path.name
            
            # 提取原始文件名
            original_name = parse_filename(filename)
            if not original_name:
                continue
            
            # 构建原始文件路径
            original_relative = relative_path.parent / original_name
            original_path = Path(BASE_IMG_DIR) / original_relative
            
            # 检查原始文件是否存在
            if not original_path.exists():
                # 原始文件不存在，删除缓存文件
                cache_file_path.unlink()
                cache_files_deleted += 1
                logger.info(f"Deleted cache (original missing): {cache_file_path}")
                continue
            
            # 检查mtime是否一致
            cache_mtime = cache_file_path.stat().st_mtime
            orig_mtime = original_path.stat().st_mtime
            
            if cache_mtime != orig_mtime:
                # mtime不一致，删除缓存文件
                cache_file_path.unlink()
                cache_files_deleted += 1
                logger.info(f"Deleted cache (mtime mismatch): {cache_file_path}")
                continue
            
            # 检查atime（访问时间）
            cache_atime = cache_file_path.stat().st_atime
            cache_age = time.time() - cache_atime
            max_age_seconds = CACHE_MAX_AGE * 24 * 60 * 60
            
            if cache_age > max_age_seconds:
                # 缓存文件太久未被访问，删除
                cache_file_path.unlink()
                cache_files_deleted += 1
                logger.info(f"Deleted cache (too old): {cache_file_path}")
                
        except Exception as e:
            errors += 1
            logger.error(f"Error processing {cache_file_path}: {str(e)}")
    
    logger.info(f"Cache cleanup completed. Checked: {cache_files_checked}, "
                f"Deleted: {cache_files_deleted}, Errors: {errors}")

if __name__ == '__main__':
    cleanup_cache()
