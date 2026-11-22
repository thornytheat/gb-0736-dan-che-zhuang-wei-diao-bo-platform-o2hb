from flask import Blueprint, jsonify

from app.modules.release import service
from app.web import body, selected, store

bp = Blueprint('release', __name__)


@bp.get('/api/release')
def show():
    snapshot = selected()
    return jsonify(id=snapshot['manifest']['id'], data=service.current(store(), snapshot))


@bp.post('/api/release/publish')
def accept():
    data = body('expected', 'orders')
    return jsonify(service.publish(store(), data['expected'], data['orders']))
