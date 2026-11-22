import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from app.clock import instant, stamp
from app.config import SAMPLES
from app.errors import DomainError
from app.manifest import canonical, digest

ORDER_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
FIELDS = {
    'inspection': {'order_id', 'truck', 'kind', 'result', 'signed_at'},
    'maintenance': {'order_id', 'truck', 'kind', 'system', 'finished_at', 'qc_signed_at'},
}
TIME_FIELDS = {'inspection': ('signed_at',), 'maintenance': ('finished_at', 'qc_signed_at')}


def validate_orders(raw, trucks):
    if not isinstance(raw, list):
        raise DomainError('BAD_ORDER')
    orders = {}
    for index, order in enumerate(raw, 1):
        kind = order.get('kind') if isinstance(order, dict) else None
        if kind not in FIELDS or set(order) != FIELDS[kind]:
            raise DomainError('BAD_ORDER', index=index)
        order_id = order['order_id']
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id) or order_id in orders:
            raise DomainError('BAD_ORDER', index=index)
        if order['truck'] not in trucks:
            raise DomainError('UNKNOWN_TRUCK', order_id=order_id)
        normalized = {'order_id': order_id, 'truck': order['truck'], 'kind': kind}
        if kind == 'inspection':
            if order['result'] not in ('pass', 'fail'):
                raise DomainError('BAD_ORDER', index=index, field='result')
            normalized['result'] = order['result']
        else:
            if not isinstance(order['system'], str) or not order['system'].strip():
                raise DomainError('BAD_ORDER', index=index, field='system')
            normalized['system'] = order['system']
        for field in TIME_FIELDS[kind]:
            value = order[field]
            if value is None and field == 'qc_signed_at':
                normalized[field] = None
                continue
            try:
                normalized[field] = stamp(instant(value))
            except DomainError:
                raise DomainError('BAD_TIME', order_id=order_id, field=field) from None
        orders[order_id] = normalized
    return orders


def _holds(items, when):
    holds = []
    if not any(order['kind'] == 'inspection' and order['result'] == 'pass' and order['signed_at'] <= when
               for order in items):
        holds.append({'code': 'INSPECTION_MISSING'})
    systems = defaultdict(list)
    for order in items:
        if order['kind'] == 'maintenance':
            systems[order['system']].append(order)
    for system in sorted(systems):
        group = systems[system]
        if not any(order['qc_signed_at'] and order['qc_signed_at'] <= when for order in group):
            holds.append({'code': 'QC_PENDING', 'system': system,
                          'orders': sorted(order['order_id'] for order in group)})
    return holds


def conclude(shift, legs, orders):
    by_truck = defaultdict(list)
    for order in orders.values():
        by_truck[order['truck']].append(order)
    plan = [dict(leg, depart=stamp(instant(leg['depart'])), arrive=stamp(instant(leg['arrive'])))
            for leg in legs]
    plan.sort(key=lambda leg: (leg['depart'], leg['id']))
    first_use = {}
    for leg in plan:
        first_use.setdefault(leg['truck'], leg['depart'])
    trucks = []
    for truck_id in sorted(shift['trucks']):
        at = first_use.get(truck_id, shift['start'])
        items = by_truck.get(truck_id, [])
        holds = _holds(items, at)
        trucks.append({'truck': truck_id, 'status': 'held' if holds else 'released', 'at': at,
                       'holds': holds,
                       'basis': {'inspection': sorted(o['order_id'] for o in items if o['kind'] == 'inspection'),
                                 'maintenance': sorted(o['order_id'] for o in items if o['kind'] == 'maintenance')}})
    state = {truck_id: {'station': truck['station'], 'at': stamp(instant(truck['available_at']))}
             for truck_id, truck in shift['trucks'].items()}
    disposed = []
    for leg in plan:
        truck_id = leg['truck']
        holds = _holds(by_truck.get(truck_id, []), leg['depart'])
        position = state[truck_id]['station'] == leg['from'] and state[truck_id]['at'] <= leg['depart']
        if not holds and position:
            disposed.append({'leg': leg, 'action': 'kept', 'truck': truck_id})
            state[truck_id] = {'station': leg['to'], 'at': leg['arrive']}
            continue
        reason = holds or [{'code': 'ROUTE_BROKEN'}]
        refs = sorted({order_id for hold in holds for order_id in hold.get('orders', [])})
        candidates = [other for other in sorted(state) if other != truck_id
                      and not _holds(by_truck.get(other, []), leg['depart'])
                      and state[other]['station'] == leg['from'] and state[other]['at'] <= leg['depart']
                      and shift['trucks'][other]['load_limit'] >= leg['count']]
        if candidates:
            other = candidates[0]
            disposed.append({'leg': leg, 'action': 'swapped', 'from_truck': truck_id, 'to_truck': other,
                             'reason': reason, 'basis': refs})
            state[other] = {'station': leg['to'], 'at': leg['arrive']}
        else:
            disposed.append({'leg': leg, 'action': 'cancelled', 'truck': truck_id,
                             'reason': reason, 'basis': refs})
    return {'trucks': trucks, 'legs': disposed}


def _version(snapshot, orders):
    body = {'run': snapshot['manifest']['id'],
            'orders': [orders[key] for key in sorted(orders)],
            'conclusion': conclude(snapshot['shift'], snapshot['dispatch']['legs'], orders)}
    return dict(body, id=digest(canonical(body).encode()),
                generated_at=stamp(datetime.now(timezone.utc)))


def current(store, snapshot):
    version = _version(snapshot, store.release_orders())
    return {'orders': version['orders'], 'conclusion': version['conclusion'],
            'version': version['id'], 'published': store.release_version(version['id']) is not None,
            'active_version': store.release_active(), 'history': store.release_history()}


def publish(store, expected, raw_orders):
    snapshot = store.load(expected)
    orders = validate_orders(raw_orders, snapshot['shift']['trucks'])
    payloads = {order_id: canonical(order) for order_id, order in orders.items()}
    version, created, previous = store.publish_release(
        expected, payloads, lambda merged: _version(snapshot, merged))
    return {'id': version['id'], 'created': created, 'changed': version['id'] != previous,
            'conclusion': version['conclusion']}


def seed(store):
    if store.release_orders():
        return
    snapshot = store.load()
    raw = json.loads((SAMPLES / 'release.json').read_bytes())
    orders = validate_orders(raw, snapshot['shift']['trucks'])
    payloads = {order_id: canonical(order) for order_id, order in orders.items()}
    store.publish_release(snapshot['manifest']['id'], payloads, lambda merged: _version(snapshot, merged))
