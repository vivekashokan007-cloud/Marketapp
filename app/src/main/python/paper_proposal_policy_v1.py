"""Permitted Paper view. Frozen action/ER research remains unchanged."""
VERSION = 'paper_permitted_buy_v1'


def view(plan):
    action = plan.get('action')
    blocked = str(action or '').startswith('SELL')
    return {'version': VERSION, 'action': 'WAIT' if blocked else action,
            'original_action': action, 'er_switch': plan.get('er_switch'),
            'status': 'POLICY_BLOCKED' if blocked else plan.get('status'),
            'reason': 'UNCOVERED_SHORT_NOT_OFFERED' if blocked else None,
            'readiness_allowed': not blocked and action in ('BUY_CE', 'BUY_PE')
                and plan.get('status') in ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT')}
