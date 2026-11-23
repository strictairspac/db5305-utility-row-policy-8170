"""整改派单的纯逻辑核心：签署对账与回执判定。

本模块不依赖数据库，所有函数接收普通字典，便于独立测试。
定下的规则：
- 差异键 = 人员编号|工单编号|判定（越权可见/应见未见），是一条差异的稳定身份。
- 复核签署时对本证据覆盖的组合对账：矩阵判定已恢复正常（授权可见/正确拦截）才撤下；
  矩阵未覆盖的组合视为没复验，一律保留。
- 同一差异键同时最多一条未关闭派单；撤下后再出现时另起新派单，代次在历史最大值上递增。
- 回执只认「派单号 + 代次」：派单已关闭或代次不符的旧回执一律拒绝，不能确认新派单。
"""

from src.constants.workflow import Dispatch

DIFFERENCE_SECTION = '差异清单'
MATRIX_SECTION = '逐工单访问矩阵'
RESOLVED_VERDICTS = {'授权可见', '正确拦截'}


def difference_key(operator_id, order_id, kind):
    return f'{operator_id}|{order_id}|{kind}'


def _section(report, title):
    for section in report.get('sections') or []:
        if section.get('title') == title:
            return section.get('rows') or []
    return []


def plan_reconcile(report, open_dispatches, generations):
    """按新签署证据计算派单动作。

    report: 证据报告（含差异清单与逐工单访问矩阵两个 section）。
    open_dispatches: 当前未关闭派单的字典列表，含 difference_key/operator_id/order_id/kind。
    generations: {差异键: 历史最大代次}（含已关闭派单），函数会就地推进新代次。
    返回 (closes, updates, opens)：
      closes  —— 复验确认差异已消失，应撤下的差异键列表。
      updates —— 仍在差异清单中，应刷新最近见证证据的差异键列表。
      opens   —— 应新开的派单字典（含代次）。
    """
    matrix = {}
    for row in _section(report, MATRIX_SECTION):
        matrix[(row.get('人员编号'), row.get('工单编号'))] = row.get('判定')

    closes, updates = [], []
    for dispatch in open_dispatches:
        verdict = matrix.get((dispatch['operator_id'], dispatch['order_id']))
        if verdict is None:
            continue  # 本次签署未覆盖该组合：没复验的仍保留
        if verdict in RESOLVED_VERDICTS or verdict != dispatch['kind']:
            closes.append(dispatch['difference_key'])
        else:
            updates.append(dispatch['difference_key'])

    still_open = {row['difference_key'] for row in open_dispatches} - set(closes)
    opens = []
    for row in _section(report, DIFFERENCE_SECTION):
        key = difference_key(row.get('人员编号'), row.get('工单编号'), row.get('判定'))
        if key in still_open:
            continue  # 该差异已有未关闭派单：重试不重复派单
        generation = generations.get(key, 0) + 1
        generations[key] = generation
        opens.append({
            'difference_key': key,
            'operator_id': row.get('人员编号'),
            'order_id': row.get('工单编号'),
            'zone': row.get('作业区域'),
            'utility': row.get('表计类型'),
            'kind': row.get('判定'),
            'generation': generation,
        })
    return closes, updates, opens


def plan_ack(dispatch, generation, actor):
    """回执判定：返回 (结论, 说明)。结论为 accept / idempotent / conflict。

    dispatch: 派单字典，含 status/generation/acked_by。
    generation: 回执声明的代次；actor: 回执人。
    """
    if dispatch['status'] == Dispatch.CLOSED:
        return 'conflict', '该派单已被复验签署撤下，晚到的回执不能确认新派单，请重新领单。'
    if dispatch['generation'] != generation:
        return 'conflict', (f"回执代次（第 {generation} 代）与当前派单代次"
                            f"（第 {dispatch['generation']} 代）不一致，请重新领单。")
    if dispatch['status'] == Dispatch.ACKNOWLEDGED:
        if dispatch.get('acked_by') == actor:
            return 'idempotent', '该派单已由本人签收，重复回执不再变更。'
        return 'conflict', f"该派单已由 {dispatch.get('acked_by')} 签收。"
    return 'accept', ''
