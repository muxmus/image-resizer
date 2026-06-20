import os
import re
import time
from pathlib import Path
import logging

# ---------- 配置 ----------
BASE_IMG_DIR = "/home/ubuntu/Server/img"
CACHE_DIR    = "/home/ubuntu/Server/image-resizer/image_cache"
CACHE_MAX_AGE = 30  # 缓存最大未访问天数

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_filename(filename):
    """从缓存文件名提取原始文件名（去掉 @尺寸 描述符）"""
    match = re.match(r'^(.*?)@((\d+)w)?_?((\d+)h)?\.([a-zA-Z]+)$', filename)
    return match.group(1) if match else None


def cleanup_cache():
    logger.info("Starting cache cleanup...")

    checked = 0
    deleted = 0
    errors  = 0

    # ── 清理 .tmp 残留文件 ──────────────────────────────────────────────────
    # 进程被 PM2/OOM killer 强杀时，原子写入的临时文件来不及被 catch 删除。
    # Nginx 不会匹配 .tmp 后缀，但仍应定期清理以免占用磁盘。
    for tmp_path in Path(CACHE_DIR).rglob('*.tmp'):
        try:
            tmp_path.unlink()
            deleted += 1
            logger.info(f"Deleted (stale tmp): {tmp_path}")
        except Exception as e:
            errors += 1
            logger.error(f"Error deleting tmp {tmp_path}: {e}")

    # ── 清理正式缓存文件 ────────────────────────────────────────────────────
    for cache_path in Path(CACHE_DIR).rglob('*@*.*'):
        try:
            checked += 1
            stat = cache_path.stat()

            # ① 空文件：处理失败时的遗留产物
            #    若不删除，Nginx 的 try_files 会命中它并向客户端返回空内容，
            #    且 Node.js 永远不会再收到该请求来重新生成。
            if stat.st_size == 0:
                cache_path.unlink()
                deleted += 1
                logger.info(f"Deleted (empty file): {cache_path}")
                continue

            relative = cache_path.relative_to(CACHE_DIR)
            original_name = parse_filename(relative.name)
            if not original_name:
                continue

            # ② 原图已删除
            orig_path = Path(BASE_IMG_DIR) / relative.parent / original_name
            if not orig_path.exists():
                cache_path.unlink()
                deleted += 1
                logger.info(f"Deleted (original missing): {cache_path}")
                continue

            # ③ 原图已更新（mtime 不一致）
            if stat.st_mtime != orig_path.stat().st_mtime:
                cache_path.unlink()
                deleted += 1
                logger.info(f"Deleted (mtime mismatch): {cache_path}")
                continue

            # ④ 超过最大未访问天数
            age_seconds = time.time() - stat.st_atime
            if age_seconds > CACHE_MAX_AGE * 86400:
                cache_path.unlink()
                deleted += 1
                logger.info(f"Deleted (not accessed for {CACHE_MAX_AGE}d): {cache_path}")

        except Exception as e:
            errors += 1
            logger.error(f"Error processing {cache_path}: {e}")

    logger.info(
        f"Cache cleanup done — checked: {checked}, deleted: {deleted}, errors: {errors}"
    )


if __name__ == '__main__':
    cleanup_cache()
