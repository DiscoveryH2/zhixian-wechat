from concurrent.futures import Future
import json
from PySide6.QtCore import QObject, Signal, Slot


class Bridge(QObject):
    response = Signal(str)
    event = Signal(str)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        controller.stateChanged.connect(lambda data: self.publish('state', data))
        controller.toast.connect(lambda message, level: self.publish('toast', {'message': message, 'level': level}))

    def publish(self, kind, data):
        self.event.emit(json.dumps({'type': kind, 'data': data}, ensure_ascii=False))

    def reply(self, ident, data=None, error=None):
        self.response.emit(json.dumps({'id': ident, 'ok': error is None, 'data': data, 'error': error}, ensure_ascii=False))

    @Slot(str)
    def request(self, raw):
        ident = None
        try:
            if len(raw) > 300000:
                raise ValueError('请求内容过长。')
            packet = json.loads(raw)
            ident = packet.get('id')
            params = packet.get('params') or {}
            if not isinstance(params, dict):
                raise ValueError('请求参数格式无效。')
            result = self.controller.handle(packet.get('method'), params)
            if isinstance(result, Future):
                def done(future):
                    try:
                        self.reply(ident, future.result())
                    except Exception as exc:
                        self.reply(ident, error=self.controller.safe_error(exc))
                result.add_done_callback(done)
            else:
                self.reply(ident, result)
        except Exception as exc:
            self.reply(ident, error=self.controller.safe_error(exc))
