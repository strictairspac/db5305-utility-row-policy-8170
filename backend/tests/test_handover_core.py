"""整改派单核心规则的单测（纯逻辑，不需要数据库）。

运行：PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
"""

import unittest

from src.constants.workflow import Dispatch
from src.services.handover_core import difference_key, plan_ack, plan_reconcile


def make_report(differences, matrix):
    return {'sections': [
        {'title': '差异清单', 'rows': differences},
        {'title': '逐工单访问矩阵', 'rows': matrix},
    ]}


def diff(operator, order, zone, kind, utility='水表'):
    return {'人员编号': operator, '工单编号': order, '作业区域': zone, '表计类型': utility, '判定': kind}


def cell(operator, order, verdict):
    return {'人员编号': operator, '工单编号': order, '判定': verdict}


class Ledger:
    """模拟数据库层的对账落库：撤下即关闭，开出追加新行。"""

    def __init__(self):
        self.open = []
        self.generations = {}
        self.next_id = 1

    def reconcile(self, report):
        closes, updates, opens = plan_reconcile(report, self.open, self.generations)
        self.open = [row for row in self.open if row['difference_key'] not in closes]
        for spec in opens:
            self.open.append({**spec, 'id': self.next_id, 'status': Dispatch.OPEN})
            self.next_id += 1
        return closes, updates, opens

    def find(self, operator, order, kind):
        key = difference_key(operator, order, kind)
        return next((row for row in self.open if row['difference_key'] == key), None)


class ReconcileTest(unittest.TestCase):
    def test_signed_differences_dispatch_to_their_zone(self):
        ledger = Ledger()
        report = make_report(
            [diff('BD-017', 'MR-703', '南堤', '越权可见'), diff('BD-026', 'MR-703', '南堤', '应见未见')],
            [])
        _, _, opens = ledger.reconcile(report)
        self.assertEqual(len(opens), 2)
        self.assertEqual({row['generation'] for row in opens}, {1})
        self.assertEqual({row['zone'] for row in opens}, {'南堤'})

    def test_retry_does_not_dispatch_twice(self):
        ledger = Ledger()
        report = make_report([diff('BD-017', 'MR-703', '南堤', '越权可见')], [])
        ledger.reconcile(report)
        _, _, opens = ledger.reconcile(report)  # 同一证据重试对账
        self.assertEqual(opens, [])
        self.assertEqual(len(ledger.open), 1)

    def test_only_signed_recheck_closes_and_untouched_items_remain(self):
        ledger = Ledger()
        first = make_report(
            [diff('BD-017', 'MR-703', '南堤', '越权可见'), diff('BD-026', 'MR-705', '南堤', '应见未见')],
            [cell('BD-017', 'MR-703', '越权可见'), cell('BD-026', 'MR-705', '应见未见')])
        ledger.reconcile(first)
        # 复验只覆盖 BD-017 的组合且已恢复；BD-026 的组合没复验，必须保留。
        recheck = make_report([], [cell('BD-017', 'MR-703', '正确拦截')])
        closes, _, opens = ledger.reconcile(recheck)
        self.assertEqual(closes, [difference_key('BD-017', 'MR-703', '越权可见')])
        self.assertEqual(opens, [])
        self.assertIsNone(ledger.find('BD-017', 'MR-703', '越权可见'))
        self.assertIsNotNone(ledger.find('BD-026', 'MR-705', '应见未见'))

    def test_reappearing_difference_gets_new_generation(self):
        ledger = Ledger()
        first = make_report([diff('BD-017', 'MR-703', '南堤', '越权可见')],
                            [cell('BD-017', 'MR-703', '越权可见')])
        ledger.reconcile(first)
        ledger.reconcile(make_report([], [cell('BD-017', 'MR-703', '正确拦截')]))  # 复验撤下
        relapse = make_report([diff('BD-017', 'MR-703', '南堤', '越权可见')],
                              [cell('BD-017', 'MR-703', '越权可见')])
        _, _, opens = ledger.reconcile(relapse)  # 同一差异撤下后又出现
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]['generation'], 2)
        self.assertNotEqual(opens[0]['id'] if 'id' in opens[0] else None, 1)

    def test_changed_verdict_closes_old_and_opens_new_kind(self):
        ledger = Ledger()
        ledger.reconcile(make_report([diff('BD-017', 'MR-703', '南堤', '越权可见')],
                                     [cell('BD-017', 'MR-703', '越权可见')]))
        # 同一组合从越权可见变成应见未见：旧差异消失撤下，新差异另开第一代。
        closes, _, opens = ledger.reconcile(make_report(
            [diff('BD-017', 'MR-703', '南堤', '应见未见')], [cell('BD-017', 'MR-703', '应见未见')]))
        self.assertEqual(closes, [difference_key('BD-017', 'MR-703', '越权可见')])
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]['kind'], '应见未见')
        self.assertEqual(opens[0]['generation'], 1)


class AckTest(unittest.TestCase):
    def dispatch(self, status=Dispatch.OPEN, generation=2, acked_by=None):
        return {'status': status, 'generation': generation, 'acked_by': acked_by}

    def test_accepts_matching_generation(self):
        outcome, _ = plan_ack(self.dispatch(), generation=2, actor='lead-nandi')
        self.assertEqual(outcome, 'accept')

    def test_late_receipt_from_old_generation_is_rejected(self):
        # 撤下后再出现的新派单是第 2 代，晚到的第 1 代旧回执确认不了它。
        closed = self.dispatch(status=Dispatch.CLOSED, generation=1)
        self.assertEqual(plan_ack(closed, generation=1, actor='lead-nandi')[0], 'conflict')
        renewed = self.dispatch(generation=2)
        self.assertEqual(plan_ack(renewed, generation=1, actor='lead-nandi')[0], 'conflict')

    def test_duplicate_receipt_is_idempotent_for_same_actor_only(self):
        acked = self.dispatch(status=Dispatch.ACKNOWLEDGED, acked_by='lead-nandi')
        self.assertEqual(plan_ack(acked, generation=2, actor='lead-nandi')[0], 'idempotent')
        self.assertEqual(plan_ack(acked, generation=2, actor='lead-beizha')[0], 'conflict')


if __name__ == '__main__':
    unittest.main()
