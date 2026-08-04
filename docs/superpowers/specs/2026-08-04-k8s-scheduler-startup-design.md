# K8S Prometheus 定时拉取启动修复设计

## 问题

`K8SPrometheusConfig.scheduled_update_enabled` 已能控制
`start_k8s_background_updater()` 是否创建后台线程，但 `python app.py` 的启动路径没有调用该函数。
因此将开关设置为 `True` 仍不会发生定时拉取。

## 范围

仅修复 `python app.py` 启动方式，不改变 Prometheus 拉取、增量窗口、预测或数据合并逻辑，
也不引入独立调度服务或多进程部署支持。

## 设计

在 `app.py` 的 `__main__` 启动路径中显式启动 K8S 后台调度器。调度器仍读取现有配置：

- `scheduled_update_enabled`：是否启用；
- `scheduled_update_interval_minutes`：后续拉取间隔；
- `scheduled_update_startup_delay_seconds`：首次拉取延迟。

调度器不放入 `create_app()`，避免应用工厂被测试或其他进程导入时产生后台线程副作用。
当 Flask debug reloader 启用时，只在实际提供服务的 reloader 子进程中启动，避免父子进程各启动一个线程。
进程正常退出时停止调度线程。

## 错误处理

保持现有调度循环行为：单次拉取异常写入日志，后台循环继续运行；关闭等待超时只记录警告，
不阻塞进程无限退出。

## 验证

增加针对启动判定的单元测试，覆盖普通启动、debug reloader 父进程和子进程。
运行相关调度器测试，并确认文档不再声称 `python app.py` 永远不会自动拉取。

