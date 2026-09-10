# 保留SQLite 3.7.17的部署说明

Python版本与其链接的SQLite版本是两回事。Python3.10.13可以链接SQLite3.7.17；`deterministic=True requires SQLite 3.8.3 or higher`来自数据库库版本，不是Python版本过低。

当前实现保留Python标准库sqlite3和现有数据库，不安装pysqlite3、不替换系统库、不通过sys.modules替换驱动。`resource_predict/sqlite_runtime.py`只在原生连接上注册普通Python标量函数读取项目需要的JSON字段，不使用JSON1扩展或deterministic标志。

## 旧语法实现

- 准确性查询不再依赖WITH、ROW_NUMBER/OVER或窗口函数。源数据库仍按URI只读连接，在临时表中筛选，以排序游标逐组选择最新预测，分批保存选中键。
- P95仍为nearest-rank，使用有序临时数据、普通聚合及行号定位。资源/模型/单位/提前量、最新版本选择、有效样本分母和±5/10pp边界不变。
- 大段basis/provenance/evaluation JSON按曲线只保存一份，不复制进每个候选点、选中点和误差行；导出时再关联。临时存储按SQLite文件模式处理，Python不一次加载全部预测点。
- 兑现报告和调配成效汇总使用普通子查询；启用评审、相邻影子批次比较采用有序游标。内部批次ID经严格整数校验后处理，避免3.7.17默认999个绑定参数上限。
- 新建索引不使用部分索引语法。成效样本使用事务内INSERT OR IGNORE加有条件UPDATE，仅允许缺失值补齐，不覆盖已有有效证据。

## 更新步骤

替换部署包中的程序文件，保留服务器现有deploy配置和outputs数据，然后重启服务。本次不需要安装或升级SQLite驱动。

```bash
source .venv/bin/activate
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

版本仍显示3.7.17是预期结果。打开“预测准确性”，兑现评估和独立历史测试都应能读取已有证据；没有证据时显示空结果，而不是新SQLite语法错误。

预测准确率现已改为轻量JSON汇总，不依赖SQLite。逐点留档和补导入命令已删除；部署后运行一次新预测即可生成汇总，无需修复或导入旧预测证据。其他业务仍使用现有SQLite3.7.17兼容实现。

## 验证边界

测试使用现有本机SQLite，并通过语法守卫禁止3.7.17不支持的查询、函数标志和写入语法，同时模拟旧版忽略query_only的行为，核验源库URI只读保护。比较了数值口径、候选/选中证据、同ID跨库、长元数据、超过999批次、回滚和重复写入。服务器仍需在其实际3.7.17环境验证；不会以本机现代SQLite版本冒充服务器版本。

此前下载但未安装的SQLite兼容wheel不属于本部署方案，也不包含在部署ZIP中。
