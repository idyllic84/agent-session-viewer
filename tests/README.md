# Integration / E2E

这里仅放仓库级集成与端到端测试。三个项目各自测试入口：

```bash
python -m unittest discover -s server/tests
python -m unittest discover -s deamon/tests
python -m unittest discover -s local/tests
```

跨项目集成（合成历史/临时 DB，含真实 loopback HTTP）：

```bash
python -m unittest discover -s tests
```

- `test_integration.py`：Server ↔ Deamon，鉴权、五种来源、增量、云端进度、故障恢复。
- `test_analysis.py`：四类消息的 wire/落库内容，时间戳、结果状态、无 thinking/附件/raw。
- `test_local_integration.py`：原本地服务子进程的启动同步/监听顺序。
- `test_layout.py`：三项目边界和数据库 Git 排除。
- `e2e/local_browser.mjs`：原本地服务真实浏览器，不读取真实历史。
- `e2e/smoke.py`：Docker + 云端任务 + 无状态 Deamon + 重启持久化。
- `e2e/browser_smoke.mjs`：Docker Viewer 真实浏览器/CSRF/分析内容显示。

```bash
node --experimental-default-type=module tests/e2e/local_browser.mjs
```

Docker E2E 的隔离卷/凭据准备见 [server/deploy/README.md](../server/deploy/README.md)。
不要对真实库/生产卷跑合成数据测试；只清理测试自己创建的临时进程/卷。
Windows 子进程均无窗口启动；凭据不通过命令行值或测试日志暴露。
