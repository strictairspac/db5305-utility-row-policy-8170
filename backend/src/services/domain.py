"""Reconcile dispatch-approved meter work orders with observed PostgreSQL access."""

from datetime import datetime

from .row_security import read_visible_orders


SPEC = {
    "name": "埠灯公服·抄表工单分域",
    "summary": "按人员核对派工确认的工单授权与数据库行级策略返回的工单，定位越权和漏放。",
    "customer": "埠灯公共服务有限公司 · 表务调度与内控组",
    "entity_labels": ["抄表工单", "权限验签", "差异清单"],
    "field_labels": {
        "audit_at": "验签时点（含时区）",
        "operators": "待验签人员",
        "operator_id": "人员编号",
        "operator_name": "姓名",
        "work_orders": "抄表工单",
        "order_id": "工单编号",
        "meter_id": "表计编号",
        "zone": "作业区域",
        "utility": "表计类型",
        "delegations": "数据库区域委派",
        "valid_from": "委派生效（含时区）",
        "valid_until": "委派到期（含时区）",
        "business_grants": "派工确认的人员工单授权",
    },
    "guidance": "录入同一批次的人员、工单和区域委派，再按复核表逐条填写人员工单授权。",
    "sample": {
        "audit_at": "2026-11-21T09:00:00+08:00",
        "operators": [
            {"operator_id": "BD-017", "operator_name": "林澄"},
            {"operator_id": "BD-026", "operator_name": "许遥"},
            {"operator_id": "BD-034", "operator_name": "乔榆"},
            {"operator_id": "BD-041", "operator_name": "程仪"},
        ],
        "work_orders": [
            {"order_id": "MR-701", "meter_id": "W-N018", "zone": "北闸", "utility": "水表"},
            {"order_id": "MR-702", "meter_id": "E-N048", "zone": "北闸", "utility": "电表"},
            {"order_id": "MR-703", "meter_id": "W-S022", "zone": "南堤", "utility": "水表"},
            {"order_id": "MR-704", "meter_id": "W-W011", "zone": "西港", "utility": "水表"},
            {"order_id": "MR-705", "meter_id": "E-S109", "zone": "南堤", "utility": "电表"},
        ],
        "delegations": [
            {
                "operator_id": "BD-017", "zone": "北闸", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
            {
                "operator_id": "BD-017", "zone": "南堤", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T10:00:00+08:00",
            },
            {
                "operator_id": "BD-026", "zone": "北闸", "utility": "电表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
            {
                "operator_id": "BD-026", "zone": "南堤", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T09:00:00+08:00",
            },
            {
                "operator_id": "BD-041", "zone": "西港", "utility": "水表",
                "valid_from": "2026-11-21T09:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
        ],
        "business_grants": [
            {"operator_id": "BD-017", "order_id": "MR-701"},
            {"operator_id": "BD-026", "order_id": "MR-702"},
            {"operator_id": "BD-026", "order_id": "MR-703"},
            {"operator_id": "BD-041", "order_id": "MR-704"},
        ],
    },
    "expect": {
        "验签人员数": 4, "核对组合数": 20, "业务授权数": 4, "数据库可见数": 4,
        "越权可见数": 1, "应见未见数": 1, "差异数": 2,
    },
}


def _records(payload, field, *columns):
    records = payload.get(field)
    if not isinstance(records, list):
        raise ValueError(f"{SPEC['field_labels'][field]}须为数组。")
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{SPEC['field_labels'][field]}第 {index} 行须为对象。")
        for column in columns:
            value = record.get(column)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{SPEC['field_labels'][field]}第 {index} 行的{SPEC['field_labels'][column]}须为非空文本。")
    return records


def _time(value, label):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is not None:
            return timestamp
    except (TypeError, ValueError):
        pass
    raise ValueError(f"{label}须使用含时区的 ISO 8601 日期时间。")


def _index(records, key):
    index = {}
    for record in records:
        value = record[key]
        if value in index:
            raise ValueError(f"{SPEC['field_labels'][key]}重复（{value}）。")
        index[value] = record
    return index


def _read_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("验签输入须为对象。")
    audit_at = _time(payload.get("audit_at"), "验签时点")
    operators = _index(_records(payload, "operators", "operator_id", "operator_name"), "operator_id")
    orders = _index(_records(payload, "work_orders", "order_id", "meter_id", "zone", "utility"), "order_id")
    if not operators:
        raise ValueError("至少需要一名待验签人员。")
    delegations = []
    for row in _records(payload, "delegations", "operator_id", "zone", "utility", "valid_from", "valid_until"):
        if row["operator_id"] not in operators:
            raise ValueError(f"区域委派引用了未知人员（{row['operator_id']}）。")
        start = _time(row["valid_from"], "委派生效时间")
        end = _time(row["valid_until"], "委派到期时间")
        if start >= end:
            raise ValueError(f"人员 {row['operator_id']} 的委派到期时间须晚于生效时间。")
        delegations.append({**row, "valid_from": start, "valid_until": end})

    for row in (*orders.values(), *delegations):
        if row["utility"] not in {"水表", "电表"}:
            raise ValueError("表计类型须为水表或电表。")

    expected = {operator_id: set() for operator_id in operators}
    for row in _records(payload, "business_grants", "operator_id", "order_id"):
        operator_id, order_id = row["operator_id"], row["order_id"]
        if operator_id not in operators or order_id not in orders:
            raise ValueError(f"业务授权引用了未知人员或工单（{operator_id} / {order_id}）。")
        expected[operator_id].add(order_id)
    return audit_at, operators, orders, delegations, expected


def validate(payload):
    _read_payload(payload)


def solve(payload, engine):
    audit_at, operators, orders, delegations, expected = _read_payload(payload)
    readings = read_visible_orders(engine, audit_at, sorted(operators), orders.values(), delegations)
    order_ids = sorted(orders)
    people, matrix, differences = [], [], []
    excess_count = missing_count = 0
    for operator_id, (role, visible) in readings.items():
        person = {"人员编号": operator_id, "姓名": operators[operator_id]["operator_name"]}
        approved = expected[operator_id]
        excess, missing = visible - approved, approved - visible
        excess_count += len(excess)
        missing_count += len(missing)
        people.append({
            **person, "数据库角色": role, "工单与委派 RLS 生效": "是",
            "业务授权工单": "、".join(sorted(approved)), "数据库可见工单": "、".join(sorted(visible)),
            "越权可见数": len(excess), "应见未见数": len(missing),
        })
        for order_id in order_ids:
            order = orders[order_id]
            if order_id in excess:
                verdict = "越权可见"
            elif order_id in missing:
                verdict = "应见未见"
            elif order_id in visible:
                verdict = "授权可见"
            else:
                verdict = "正确拦截"
            row = {
                **person, "工单编号": order_id, "表计编号": order["meter_id"],
                "作业区域": order["zone"], "表计类型": order["utility"],
                "业务授权": "是" if order_id in approved else "否",
                "数据库可见": "是" if order_id in visible else "否", "判定": verdict,
            }
            matrix.append(row)
            if order_id in excess or order_id in missing:
                differences.append(row)

    if differences:
        summary = f"验签发现 {excess_count} 组越权可见和 {missing_count} 组应见未见，请复核区域委派。"
    elif not orders:
        summary = "本批没有抄表工单，已用受限角色验证空可见集。"
    else:
        summary = f"验签通过，{len(operators)} 名人员的数据库可见集与派工授权一致。"
    return {
        "summary": summary,
        "metrics": {
            "验签人员数": len(operators), "核对组合数": len(matrix),
            "业务授权数": sum(map(len, expected.values())),
            "数据库可见数": sum(len(visible) for _, visible in readings.values()),
            "越权可见数": excess_count, "应见未见数": missing_count, "差异数": len(differences),
        },
        "sections": [
            {"title": "人员可见集", "rows": people},
            {"title": "差异清单", "rows": differences},
            {"title": "逐工单访问矩阵", "rows": matrix},
        ],
    }
