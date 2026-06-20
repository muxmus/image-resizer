/**
 * PM2 配置 —— 替代原版的 Gunicorn
 *
 * 生产部署：
 *   npm install -g pm2
 *   pm2 start ecosystem.config.js
 *   pm2 save && pm2 startup   # 开机自启
 *
 * 查看状态：pm2 status / pm2 logs image-resizer / pm2 monit
 */

module.exports = {
  apps: [{
    name:        'image-resizer',
    script:      './app.js',

    // 单实例 fork 模式。
    // 原 cluster 模式开了 2 个 worker，4K AVIF 转换峰值内存超过 256MB 触发
    // max_memory_restart，PM2 强杀进程导致写了一半的临时文件无法被 catch 清理。
    // 改为单实例后同一时间只有一个转换任务，内存可预测；
    // jemalloc 已接管内存释放，不需要 PM2 的内存监控兜底。
    instances:   1,
    exec_mode:   'fork',

    // 环境变量
    env: {
      NODE_ENV: 'production',

      // 限制 glibc malloc arena 数量。
      // glibc 默认每个线程有独立 arena（最多 8×核数 个），释放的内存留在 arena 里不归还 OS。
      // 设为 2 后所有线程共享最多 2 个 arena，内存水位线大幅下降。
      MALLOC_ARENA_MAX: '2',
    },

    // jemalloc（通过 LD_PRELOAD 加载）已接管内存管理，空闲内存会主动归还 OS，
    // 不需要 PM2 的 max_memory_restart 兜底；保留该项反而会在转换大图时误杀进程。

    // 日志
    out_file:    '/home/ubuntu/Server/image-resizer/logs/out.log',
    error_file:  '/home/ubuntu/Server/image-resizer/logs/err.log',
    merge_logs:  true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
  }],
};
