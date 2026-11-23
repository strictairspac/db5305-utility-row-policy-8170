"""Replay meter-order access in a disposable PostgreSQL transaction."""

from uuid import uuid4

from sqlalchemy import text


def _check_sql_preconditions(conn):
    if int(conn.exec_driver_sql("SHOW server_version_num").scalar_one()) < 160000:
        raise ValueError("工单 RLS 验签需要 PostgreSQL 16 或更新版本。")
    status = conn.exec_driver_sql("""
        SELECT current_user = session_user AS baseline_role,
               rolsuper OR rolcreaterole AS can_create_roles,
               has_database_privilege(current_database(), 'CREATE') AS can_create_schema,
               current_setting('transaction_read_only') = 'off' AS writable
        FROM pg_catalog.pg_roles WHERE rolname = current_user
    """).mappings().one()
    if not status["baseline_role"]:
        raise ValueError("工单 RLS 验签连接须以会话用户进入，且未预先切换角色。")
    if not (status["can_create_roles"] and status["can_create_schema"] and status["writable"]):
        raise ValueError("工单 RLS 验签需要可写连接、数据库 CREATE 权限及 CREATEROLE 权限或超级用户。")


def _prepare_audit(conn, schema, roles, audit_at, orders, delegations):
    orders_table = f"{schema}.work_orders"
    delegation_table = f"{schema}.delegations"
    clock_table = f"{schema}.audit_clock"
    conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
    conn.exec_driver_sql(f"""
        CREATE TABLE {orders_table} (
            order_id text PRIMARY KEY, zone text NOT NULL, utility text NOT NULL
        )
    """)
    conn.exec_driver_sql(f"""
        CREATE TABLE {delegation_table} (
            reader_role name NOT NULL, zone text NOT NULL, utility text NOT NULL,
            valid_from timestamptz NOT NULL, valid_until timestamptz NOT NULL,
            CHECK (valid_from < valid_until)
        )
    """)
    conn.exec_driver_sql(f"CREATE TABLE {clock_table} (audit_at timestamptz NOT NULL)")
    conn.execute(text(f"INSERT INTO {clock_table} VALUES (:audit_at)"), {"audit_at": audit_at})
    order_rows = list(orders)
    if order_rows:
        conn.execute(text(f"""
            INSERT INTO {orders_table} (order_id, zone, utility) VALUES (:order_id, :zone, :utility)
        """), order_rows)
    if delegations:
        conn.execute(text(f"""
            INSERT INTO {delegation_table} (reader_role, zone, utility, valid_from, valid_until)
            VALUES (:reader_role, :zone, :utility, :valid_from, :valid_until)
        """), [{**row, "reader_role": roles[row["operator_id"]]} for row in delegations])

    for role in roles.values():
        conn.exec_driver_sql(f"""
            CREATE ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
        """)
        conn.exec_driver_sql(f"GRANT {role} TO CURRENT_USER WITH INHERIT FALSE, SET TRUE")
    readers = ", ".join(roles.values())
    tables = f"{orders_table}, {delegation_table}, {clock_table}"
    conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
    conn.exec_driver_sql(f"REVOKE ALL ON {tables} FROM PUBLIC")
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA {schema} TO {readers}")
    conn.exec_driver_sql(f"GRANT SELECT ON {tables} TO {readers}")

    conn.exec_driver_sql(f"""
        CREATE POLICY own_delegations ON {delegation_table} FOR SELECT TO {readers}
        USING (reader_role = current_user)
    """)
    conn.exec_driver_sql(f"""
        CREATE POLICY delegated_orders ON {orders_table} FOR SELECT TO {readers}
        USING (EXISTS (
            SELECT 1 FROM {delegation_table} AS delegation CROSS JOIN {clock_table} AS clock
            WHERE delegation.zone = work_orders.zone
              AND delegation.utility = work_orders.utility
              AND delegation.valid_from <= clock.audit_at
              AND clock.audit_at < delegation.valid_until
        ))
    """)
    for table in (orders_table, delegation_table):
        conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def read_visible_orders(engine, audit_at, operator_ids, orders, delegations):
    if engine.dialect.name != "postgresql":
        raise ValueError("工单 RLS 验签需要 PostgreSQL 连接。")
    schema = f"gb5305_{uuid4().hex}"
    roles = {operator_id: f"{schema}_r{i}" for i, operator_id in enumerate(operator_ids, 1)}
    orders_table = f"{schema}.work_orders"
    delegation_table = f"{schema}.delegations"
    readings = {}
    # Override engine defaults so even an AUTOCOMMIT engine runs the DDL in a real transaction.
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as conn:
        transaction = conn.begin()
        try:
            conn.exec_driver_sql("SET LOCAL search_path = pg_catalog")
            _check_sql_preconditions(conn)
            conn.exec_driver_sql("SET LOCAL row_security = on")
            _prepare_audit(conn, schema, roles, audit_at, orders, delegations)
            for operator_id, role in roles.items():
                conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
                evidence = conn.execute(text("""
                    SELECT current_user AS effective_role,
                           row_security_active(CAST(:orders_table AS regclass)) AS orders_rls,
                           row_security_active(CAST(:delegation_table AS regclass)) AS delegations_rls
                """), {"orders_table": orders_table, "delegation_table": delegation_table}).mappings().one()
                if evidence["effective_role"] != role or not (evidence["orders_rls"] and evidence["delegations_rls"]):
                    raise RuntimeError(f"人员 {operator_id} 的受限角色或 RLS 未生效，已终止验签。")
                visible = conn.exec_driver_sql(f"SELECT order_id FROM {orders_table}").scalars()
                readings[operator_id] = (evidence["effective_role"], set(visible))
        finally:
            transaction.rollback()
    return readings
