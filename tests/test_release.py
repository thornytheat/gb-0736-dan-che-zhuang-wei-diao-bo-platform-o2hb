from app.modules.release.service import conclude, validate_orders

QC_SUPPLEMENT = {'order_id': 'W-0213-QC', 'truck': 'T-02', 'kind': 'maintenance', 'system': 'brakes',
                 'finished_at': '2026-11-20T07:40:00+08:00', 'qc_signed_at': '2026-11-20T14:20:00+08:00'}


def release(client, run_id):
    return client.get(f'/api/release?run={run_id}')


def publish(client, run_id, orders):
    return client.post('/api/release/publish', json={'expected': run_id, 'orders': orders})


def test_seeded_conclusion_holds_truck_until_qc_signs(client, run_id):
    response = release(client, run_id)
    assert response.status_code == 200 and response.json['id'] == run_id
    data = response.json['data']
    assert data['published'] is True and data['version'] == data['active_version']
    trucks = {row['truck']: row for row in data['conclusion']['trucks']}
    assert trucks['T-01']['status'] == 'released'
    assert trucks['T-01']['basis'] == {'inspection': ['D-0901'], 'maintenance': []}
    held = trucks['T-02']
    assert held['status'] == 'held' and held['at'] == '2026-11-20T01:01:00Z'
    assert held['holds'] == [{'code': 'QC_PENDING', 'system': 'brakes', 'orders': ['W-0213']}]
    assert held['basis'] == {'inspection': ['D-0902'], 'maintenance': ['W-0213']}
    legs = {row['leg']['id']: row for row in data['conclusion']['legs']}
    assert legs['M-01']['action'] == 'kept' and legs['M-01']['truck'] == 'T-01'
    cancelled = legs['M-02']
    assert cancelled['action'] == 'cancelled' and cancelled['truck'] == 'T-02'
    assert cancelled['basis'] == ['W-0213']
    assert cancelled['reason'][0]['code'] == 'QC_PENDING'
    assert (cancelled['leg']['from'], cancelled['leg']['to'], cancelled['leg']['count']) == ('003', '001', 2)
    assert len(data['history']) == 1


def test_same_supplement_retry_never_adds_a_second_release(client, run_id):
    first = publish(client, run_id, [QC_SUPPLEMENT])
    assert first.status_code == 200
    assert (first.json['created'], first.json['changed']) == (True, True)
    for _ in range(2):
        retry = publish(client, run_id, [QC_SUPPLEMENT])
        assert retry.json == {'id': first.json['id'], 'created': False, 'changed': False,
                              'conclusion': first.json['conclusion']}
    data = release(client, run_id).json['data']
    assert len(data['orders']) == 4 and len(data['history']) == 2
    assert data['version'] == first.json['id'] == data['active_version']


def test_late_qc_keeps_cancelled_leg_and_its_basis(client, run_id, app):
    morning = release(client, run_id).json['data']['version']
    publish(client, run_id, [QC_SUPPLEMENT])
    data = release(client, run_id).json['data']
    cancelled = {row['leg']['id']: row for row in data['conclusion']['legs']}['M-02']
    assert cancelled['action'] == 'cancelled'
    assert cancelled['basis'] == ['W-0213', 'W-0213-QC']
    db = app.extensions['shift_store']
    archived = db.release_version(morning)
    assert archived['conclusion']['legs'][1]['action'] == 'cancelled'
    assert archived['conclusion']['legs'][1]['basis'] == ['W-0213']
    assert [row['id'] for row in db.release_history()] != [morning]
    assert db.release_version(morning)['conclusion']['legs'][1]['leg']['id'] == 'M-02'


def test_failed_publish_keeps_previous_version_and_orders(client, run_id, app):
    before = release(client, run_id).json['data']
    db = app.extensions['shift_store']
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER stop_release BEFORE UPDATE ON release_active BEGIN SELECT RAISE(ABORT, 'test'); END")
    response = publish(client, run_id, [QC_SUPPLEMENT])
    assert (response.status_code, response.json['code']) == (503, 'STORE_UNAVAILABLE')
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM release_versions').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM release_orders').fetchone() == (3,)
    after = release(client, run_id).json['data']
    assert after['active_version'] == before['version'] == after['version']
    assert after['conclusion'] == before['conclusion']


def test_same_order_id_with_different_content_conflicts(client, run_id):
    changed = dict(QC_SUPPLEMENT, order_id='W-0213')
    response = publish(client, run_id, [changed])
    assert (response.status_code, response.json['code']) == (409, 'ORDER_CONFLICT')
    assert response.json['details']['order_id'] == 'W-0213'
    assert len(release(client, run_id).json['data']['orders']) == 3


def test_stale_run_and_unknown_run_are_rejected(client, run_id):
    missing = publish(client, 'no-such-run', [])
    assert (missing.status_code, missing.json['code']) == (404, 'RUN_NOT_FOUND')
    moved = client.post('/api/dispatch/publish', json={'expected': run_id, 'legs': []}).json['id']
    stale = publish(client, run_id, [])
    assert (stale.status_code, stale.json['code']) == (409, 'STALE_RUN')
    assert stale.json['details']['current'] == moved
    assert release(client, moved).json['data']['published'] is False


def test_bad_order_shapes_have_structured_errors(client, run_id):
    assert publish(client, run_id, {'order_id': 'X'}).json['code'] == 'BAD_ORDER'
    assert publish(client, run_id, [{'order_id': 'X', 'kind': 'inspection'}]).json['code'] == 'BAD_ORDER'
    unknown = dict(QC_SUPPLEMENT, order_id='W-9', truck='T-99')
    assert publish(client, run_id, [unknown]).json['code'] == 'UNKNOWN_TRUCK'
    bad_time = dict(QC_SUPPLEMENT, order_id='W-10', qc_signed_at='2026-11-20 14:20')
    response = publish(client, run_id, [bad_time])
    assert (response.json['code'], response.json['details']['field']) == ('BAD_TIME', 'qc_signed_at')
    missing_qc_field = {'order_id': 'W-11', 'truck': 'T-01', 'kind': 'maintenance',
                        'system': 'brakes', 'finished_at': '2026-11-20T07:40:00+08:00'}
    assert publish(client, run_id, [missing_qc_field]).json['code'] == 'BAD_ORDER'
    assert len(release(client, run_id).json['data']['orders']) == 3


SHIFT = {'start': '2026-11-20T00:00:00Z',
         'trucks': {'T-1': {'station': '001', 'load_limit': 4, 'available_at': '2026-11-20T00:00:00Z'},
                    'T-2': {'station': '001', 'load_limit': 2, 'available_at': '2026-11-20T00:00:00Z'}}}


def leg(leg_id, truck, depart, arrive, count=1, origin='001', dest='002'):
    return {'id': leg_id, 'truck': truck, 'from': origin, 'to': dest, 'depart': depart, 'arrive': arrive, 'count': count}


def inspection(order_id, truck, signed_at):
    return {'order_id': order_id, 'truck': truck, 'kind': 'inspection', 'result': 'pass', 'signed_at': signed_at}


def test_released_truck_covers_held_trucks_leg():
    legs = [leg('L-1', 'T-1', '2026-11-20T01:00:00Z', '2026-11-20T01:10:00Z', count=2)]
    orders = {'D-2': inspection('D-2', 'T-2', '2026-11-20T00:30:00Z')}
    result = conclude(SHIFT, legs, orders)
    assert result['trucks'][0]['status'] == 'held'
    assert result['trucks'][0]['holds'] == [{'code': 'INSPECTION_MISSING'}]
    swapped = result['legs'][0]
    assert swapped['action'] == 'swapped' and swapped['to_truck'] == 'T-2'
    assert swapped['reason'] == [{'code': 'INSPECTION_MISSING'}] and swapped['basis'] == []


def test_swap_respects_load_limit_and_falls_back_to_cancel():
    legs = [leg('L-1', 'T-1', '2026-11-20T01:00:00Z', '2026-11-20T01:10:00Z', count=3)]
    orders = {'D-2': inspection('D-2', 'T-2', '2026-11-20T00:30:00Z')}
    assert conclude(SHIFT, legs, orders)['legs'][0]['action'] == 'cancelled'


def test_qc_signed_mid_shift_releases_later_legs_but_route_stays_broken():
    shift = {'start': SHIFT['start'],
             'trucks': {'T-1': {'station': '001', 'load_limit': 4, 'available_at': '2026-11-20T00:00:00Z'}}}
    legs = [leg('L-1', 'T-1', '2026-11-20T01:00:00Z', '2026-11-20T01:10:00Z'),
            leg('L-2', 'T-1', '2026-11-20T03:00:00Z', '2026-11-20T03:10:00Z', origin='002', dest='001')]
    orders = {'D-1': inspection('D-1', 'T-1', '2026-11-20T00:30:00Z'),
              'W-1': {'order_id': 'W-1', 'truck': 'T-1', 'kind': 'maintenance', 'system': 'brakes',
                      'finished_at': '2026-11-20T00:40:00Z', 'qc_signed_at': '2026-11-20T02:00:00Z'}}
    first, second = conclude(shift, legs, orders)['legs']
    assert first['action'] == 'cancelled' and first['basis'] == ['W-1']
    assert second['action'] == 'cancelled' and second['reason'] == [{'code': 'ROUTE_BROKEN'}]


def test_validate_orders_normalizes_offsets_for_idempotent_retry():
    trucks = SHIFT['trucks']
    order = dict(QC_SUPPLEMENT, truck='T-1')
    one = validate_orders([order], trucks)
    two = validate_orders([dict(order, qc_signed_at='2026-11-19T22:20:00-08:00')], trucks)
    assert one == two
