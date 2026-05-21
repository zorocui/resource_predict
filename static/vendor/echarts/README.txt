将 ECharts 的离线文件放到当前目录：

目标文件：
- static/vendor/echarts/echarts.min.js

建议版本：
- ECharts 5.x（与当前页面写法兼容）

说明：
- 内网环境无法访问 CDN，页面改为本地静态文件加载。
- 若该文件缺失，页面会显示“ECharts 未加载”提示。
