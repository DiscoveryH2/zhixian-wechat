"""Cancelable model processes and daemon-only native helper tasks."""
import multiprocessing
import threading
from concurrent.futures import Future


def _model_job(pipe, kind, payload):
    try:
        from core.engine import analyze, test_connection
        result = test_connection(payload) if kind == 'test_connection' else analyze(**payload)
        pipe.send((True, result))
    except Exception as exc:
        config = payload if kind == 'test_connection' else payload.get('config', {})
        error = str(exc)[:1200] or '模型任务未完成。'
        for name in ('api_key', 'reply_api_key', 'weflow_token'):
            value = config.get(name)
            if value:
                error = error.replace(value, '[已隐藏]')
        pipe.send((False, error))
    finally:
        pipe.close()


class ModelTasks:
    def __init__(self):
        self.context = multiprocessing.get_context('spawn')
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False

    def submit(self, kind, payload):
        with self.lock:
            if self.closed:
                raise RuntimeError('应用正在关闭。')
            if len(self.jobs) >= 3:
                raise RuntimeError('已有模型任务正在运行，请稍后再试。')
            recv, send = self.context.Pipe(duplex=False)
            future = Future()
            process = self.context.Process(target=_model_job, args=(send, kind, payload), daemon=True)
            process.start()
            send.close()
            self.jobs[id(future)] = (process, recv, future)
        def watch():
            try:
                if not recv.poll(180):
                    process.terminate()
                    raise RuntimeError('模型任务超时，请检查网络与服务状态。')
                success, value = recv.recv()
                if not future.done():
                    future.set_result(value) if success else future.set_exception(RuntimeError(value))
            except (EOFError, OSError):
                if not future.done():
                    future.set_exception(RuntimeError('模型任务已停止。'))
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                recv.close()
                process.join(timeout=1)
                with self.lock:
                    self.jobs.pop(id(future), None)
        threading.Thread(target=watch, name='zhixian-model-result', daemon=True).start()
        return future

    def close(self):
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
            for process, recv, future in jobs:
                future.cancel()
                if process.is_alive():
                    process.terminate()
            self.jobs.clear()
        for process, _, _ in jobs:
            process.join(timeout=.5)


class HelperTasks:
    def __init__(self):
        self.closed = False
        self.slots = threading.BoundedSemaphore(3)

    def submit(self, fn, *args):
        if self.closed or not self.slots.acquire(blocking=False):
            raise RuntimeError('正在处理上一个操作，请稍后再试。')
        future = Future()
        def work():
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
            finally:
                self.slots.release()
        threading.Thread(target=work, name='zhixian-native-helper', daemon=True).start()
        return future

    def close(self):
        self.closed = True
