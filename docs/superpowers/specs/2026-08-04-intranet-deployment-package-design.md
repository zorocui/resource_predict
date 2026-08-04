# 内网部署精简打包设计

## 目标

提供一个可在 Windows 工作区直接双击运行的一键打包入口，生成可复制到内网服务器的 ZIP 部署包。部署包保持项目原有相对目录结构，只包含应用运行所需文件，不携带开发环境、测试、文档、缓存、本地输出或集群凭据。

内网服务器已经具备 Python 依赖，打包器不下载 wheel、不复制 `.venv`，也不负责安装依赖。

## 使用方式与产物

仓库根目录新增中文命名的 `.bat` 双击入口。入口调用本项目可用的 Python 解释器执行独立打包模块；优先使用 `.venv\Scripts\python.exe`，不存在时再尝试 PATH 中的 `python`。窗口显示成功或失败结果，并在双击运行时暂停，方便查看输出。

成功后在仓库根目录的 `dist/` 下生成：

```text
resource_predict_YYYYMMDD_HHMMSS.zip
```

ZIP 内使用 `resource_predict/` 作为唯一顶层目录，下面的文件继续保持仓库内的相对路径。例如 `templates/index.html` 在压缩包中为 `resource_predict/templates/index.html`。

## 运行文件白名单

打包器采用明确白名单，不复制整个工作区后再删除。允许进入部署包的内容只有：

- 根目录入口：`app.py`、`generate_forecasts.py`、`ingest_k8s_workloads.py`；
- 依赖声明：`requirements.txt`；
- Python 应用包：`resource_predict/**`；
- 页面模板：`templates/**`；
- 前端资源：`static/**`，包括离线运行所需的 `static/vendor/echarts/echarts.min.js`；
- 运行配置：`deploy/runtime_config.json`；
- 示例配置：`deploy/*.example.json`。

当前运行配置 `deploy/runtime_config.json` 必须原样保留。以下现有文件不进入部署包：

- `deploy/clusters.json`；
- `deploy/k8s_prometheus_clusters.json`；
- 遗留的 `deploy/forecast_config.json`。

因此部署包保留当前系统运行参数，但不携带真实集群控制节点、Prometheus 地址、账号或令牌。集群配置由部署人员进入页面后填写。

## 排除规则

即使位于白名单目录中，以下内容也必须排除：

- 目录：`__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`；
- 文件：`*.pyc`、`*.pyo`、日志文件、临时文件和编辑器备份文件；
- 操作系统元数据：`.DS_Store`、`Thumbs.db`。

白名单之外的 `.git`、`.venv`、`.vscode`、`.worktrees`、`.codex_tmp`、`.qoder`、`.playwright-cli`、`outputs`、`tests`、`docs`、`benchmarks`、`tools`、开发依赖和仓库说明文件自然不会进入部署包。

`dist/` 本身也不在白名单内，重复打包不会将旧部署包嵌套到新 ZIP。

## 组件边界

### 双击入口

`.bat` 只负责定位仓库目录、选择 Python 解释器、调用打包器、传递退出码和展示结果，不实现文件筛选逻辑。

### Python 打包器

Python 模块负责：

1. 根据白名单收集源文件；
2. 对每个候选路径应用缓存和临时文件排除规则；
3. 将文件以稳定的相对路径写入临时 ZIP；
4. 校验 ZIP 清单；
5. 校验通过后原子移动到最终文件名。

收集和校验函数保持无界面、可单元测试；命令行入口只负责打印中文摘要并返回退出码。

## 校验与错误处理

生成 ZIP 前必须确认以下文件存在，否则失败：

- `app.py`；
- `requirements.txt`；
- `resource_predict/__init__.py`；
- `templates/index.html`；
- `static/js/index.js`；
- `static/css/index.css`；
- `static/vendor/echarts/echarts.min.js`；
- `deploy/runtime_config.json`。

写入完成后重新读取 ZIP 清单并确认：

- 所有必需文件位于 `resource_predict/` 顶层目录下的正确位置；
- `deploy/runtime_config.json` 已包含；
- 不存在 `__pycache__`、`.pyc`、`outputs`、测试、文档、开发环境或真实集群配置；
- 不存在绝对路径和 `..` 路径段；
- ZIP 至少包含一个文件。

打包期间使用同目录临时文件。任何收集、读取、写入或校验错误都会删除临时文件、返回非零退出码，并且不会覆盖已有成功部署包。

## 测试

自动测试在临时项目目录中构造最小运行树，覆盖：

- 保留原相对目录结构和唯一顶层目录；
- 包含 `runtime_config.json`、入口文件、应用源码、模板和静态 vendor 文件；
- 排除真实集群配置、遗留预测配置、缓存、输出、测试和开发文件；
- 缺少必需文件时失败且不留下最终 ZIP；
- 归档清单校验能够拒绝禁止路径。

实现完成后运行打包脚本生成真实 ZIP，并列出归档内容验证。随后执行项目 Python 全量回归检查，并删除 `.venv` 外产生的 `__pycache__`。

