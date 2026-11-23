# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 整改交接（本次变更）

新增 `rectification_dispatches`（整改派单）与 `handover_events`（交接事件）两表，并为 `accounts` 增加 `zone` 列（区线班长账号的区域绑定）。`rectification_dispatches.open_key` 在未关闭时等于差异键、关闭时置空，其唯一约束保证同一差异同时最多一条未关闭派单。演示环境升级请重建数据卷（`docker compose down -v` 后重新 `up`）以获得新表结构。
