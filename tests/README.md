# Integration / E2E

仓库级集成与端到端测试。两个应用各自的测试入口：

```bash
python -m unittest discover -s local/tests
python -m unittest discover -s server/tests
```

跨端集成（合成历史 / 临时数据库，含真实 loopback HTTP）：

```bash
python -m unittest discover -s tests
```

- `test_integration.py`：本地导入 → 控制台连接 → 上传 → 云端；响应/请求丢失、重启、
  Key 轮换/撤销、两台电脑隔离、云端备份恢复、动态批次、内容口径。
- `test_analysis.py`：五种来源的四类消息在本地库、上传字节和云端库中一致；时间戳、
  结果状态，无 thinking / 附件 / 原始记录。
- `test_migration.py`：旧完整历史库（只读）→ 本地分析库。
- `test_local_integration.py`：本地服务子进程先导入再监听、单实例锁。
- `test_layout.py`：两应用边界、数据库/密钥 Git 排除。
- `e2e/local_browser.mjs`：真实浏览器操作本地查看页和云同步控制台（合成数据 + 进程内云端）。
- `e2e/smoke.py`：Docker 云端 + 真实本地服务子进程 + 重启持久化 + Agent Key 读取历史。
- `e2e/browser_smoke.mjs`：Docker 云端页面真实浏览器：密码登录、Key 控制台、CSRF、机器徽标/筛选/标签、日期范围、深链接。

```bash
node --experimental-default-type=module tests/e2e/local_browser.mjs
```

Docker E2E 的隔离卷/凭据准备见 [server/deploy/README.md](../server/deploy/README.md)。
不要对真实库/生产卷跑合成数据测试；只清理测试自己创建的临时进程/卷。
Windows 子进程均无窗口启动；密码、Key 不通过命令行值或测试日志暴露。
