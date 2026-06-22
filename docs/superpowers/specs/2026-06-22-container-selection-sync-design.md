# Container 选择与结论区同步设计

## 目标

K8S Workload 详情默认展示排序后的第一个 container。指标图中的 container 选择器与“建议结论”区域共享同一个资源级选择；用户切换 container 后，结论区全部指标立即同步到该 container。

## 状态模型

- 每个资源只保存一个当前 container，不再维护或读取按指标区分的 container 选择。
- 首次打开资源且没有已保存选择时，从该资源可用 container 名称的排序结果中选择第一个。
- 如果已保存的 container 不再存在，则回退到当前排序结果中的第一个。
- 切换指标只改变当前指标，不改变当前 container。

## 渲染流程

1. 详情资源数据到达后，从 `spec.containers`、`spec.containers_observed` 和已加载的 `container_charts` 汇总可用 container。
2. 图表、图表标题/单位、container 选择器和结论区都通过同一个资源级选择函数取得 container 名称。
3. 结论区不再以某个指标的图表数据是否已经加载作为显示 container 名称的前提；因此首次打开时四个指标行都会显示默认 container。
4. 点击图表 container 按钮时更新资源级选择，随后重新渲染结论区、选择器和当前图表；放大图表保持相同选择。

## 兼容与边界

- VM 资源行为不变。
- 没有 container 的 K8S 资源继续使用 Workload 级指标和标签。
- 某个 container 缺少特定指标数据时，名称仍保持一致；统计值沿用现有缺失值回退规则，图表沿用现有无数据提示。
- 不修改后端输出结构或 K8S 规格数据。

## 验证

- 打开含 `app`、`sidecar` 的 Workload，默认图表与四个结论指标均显示排序首位 `app`。
- 切换到 `sidecar` 后，图表选中态、图表数据和四个结论指标均同步为 `sidecar`。
- 切换 CPU/内存及 Request/Limit 指标后，container 保持不变。
- 重新选择资源时，各资源的选择互不干扰；已选 container 缺失时安全回退。
