"""知弦 PC — native Windows shell, offline UI, local capture and configurable Jev API."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from pathlib import Path


def locations():
    if getattr(sys, 'frozen', False):
        root = Path(sys.executable).parent
        assets = Path(sys._MEIPASS)
    else:
        root = Path(__file__).resolve().parents[1]
        assets = root
    return root, assets


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description='知弦 PC 实时微信对话助手')
    parser.add_argument('--demo', action='store_true', help='明确标记的合成数据界面预览，不读取微信，不调用模型')
    parser.add_argument('--screenshot', help='Save the rendered application UI then exit (demo only).')
    parser.add_argument('--quit-after', type=int, default=0, help='Exit after N seconds for startup smoke testing.')
    parser.add_argument('--data-dir', help='Override portable data directory.')
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--self-test-report', help='Run synthetic, offline portable diagnostics and write a JSON report.')
    parser.add_argument('--start-capture', action='store_true', help='Start capture immediately after opening the configured app.')
    parser.add_argument('--status-report', help='Write aggregate status only, without messages, titles or keys.')
    args = parser.parse_args()
    if args.self_test_report:
        from desk.selftest import run_self_test
        return run_self_test(args.self_test_report)
    if args.screenshot and not args.demo:
        parser.error('--screenshot requires --demo to avoid saving private conversation screenshots.')
    root, assets = locations()
    data = Path(args.data_dir) if args.data_dir else root / ('work/demo-data' if args.demo else 'data')
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-logging')

    from PySide6.QtCore import QTimer, QUrl, Qt
    from PySide6.QtGui import QAction, QIcon, QDesktopServices
    from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from desk.controller import Controller
    from desk.bridge import Bridge

    app = QApplication(sys.argv[:1])
    from desk.version import VERSION
    app.setApplicationName('知弦 PC')
    app.setOrganizationName('Zhixian')
    app.setApplicationVersion(VERSION)
    icon = QIcon(str(assets / 'ui' / 'icon.ico'))
    app.setWindowIcon(icon)
    controller = Controller(data)
    if args.status_report:
        def record_status(snapshot):
            session = snapshot.get('current_session') or {}
            analysis = snapshot.get('analysis') or {}
            messages = session.get('messages') or []
            summary = {'capture': snapshot['status']['capture'], 'analysis': snapshot['status']['analysis'],
                       'detail': snapshot['status']['detail'],
                       'message_count': len(messages), 'sides': sorted({m.get('side', '') for m in messages}),
                       'candidate_count': len(analysis.get('candidates') or []),
                       'judgment_count': len(analysis.get('answers') or {}),
                       'latency_ms': analysis.get('latency_ms'), 'warning': analysis.get('warning'),
                       'error': snapshot['status']['last_error']}
            import json
            Path(args.status_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        controller.stateChanged.connect(record_status)

    class LocalPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navtype, is_main_frame):
            if is_main_frame and url.scheme() not in ('file', 'qrc', 'about'):
                if navtype == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in ('https', 'http'):
                    QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, navtype, is_main_frame)

        def javaScriptConsoleMessage(self, level, message, line, source):
            # UI exceptions omit full message to avoid accidental user content in console logs.
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                print(f'UI error at line {line}', file=sys.stderr)

    class Window(QMainWindow):
        def closeEvent(self, event):
            controller.close()
            event.accept()

    window = Window()
    window.setWindowTitle('知弦 · 实时对话助手')
    window.setWindowIcon(icon)
    window.resize(1280, 850)
    window.setMinimumSize(500, 640)
    window.setStyleSheet('QMainWindow { background: #f3f5f5; }')
    view = QWebEngineView(window)
    # Off-the-record profile: no browser credentials, cookies or chat cache on disk.
    profile = QWebEngineProfile(view)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    page = LocalPage(profile, view)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    view.setPage(page)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    bridge = Bridge(controller)
    channel = QWebChannel(page)
    channel.registerObject('bridge', bridge)
    page.setWebChannel(channel)
    window.setCentralWidget(view)

    tray = QSystemTrayIcon(icon, window)
    menu = QMenu()
    for label, fn in [('打开知弦', lambda: (window.showNormal(), window.activateWindow())),
                      ('暂停采集', lambda: controller.handle('pause_capture', {})),
                      ('退出', app.quit)]:
        action = QAction(label, menu)
        action.triggered.connect(fn)
        menu.addAction(action)
    tray.setContextMenu(menu)
    tray.setToolTip('知弦 · 发送由你决定')
    tray.activated.connect(lambda reason: window.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    def window_action(kind, value):
        if kind == 'compact':
            window.resize(520, 780) if value else window.resize(1280, 850)
            window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(value) or controller.store.config['always_on_top'])
            window.show()
        elif kind == 'always_on_top':
            window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(value))
            window.show()
        elif kind == 'quit':
            app.quit()
    controller.windowAction.connect(window_action)
    app.aboutToQuit.connect(controller.close)
    app.aboutToQuit.connect(tray.hide)

    url = QUrl.fromLocalFile(str(assets / 'ui' / 'index.html'))
    if args.demo:
        # Screenshot mode targets the actual workspace; normal demo launch
        # still plays the complete cinematic introduction.
        url.setQuery('demo=1&intro=0' if args.screenshot else 'demo=1')
    view.load(url)
    window.show()
    if args.start_capture and not args.demo:
        QTimer.singleShot(1200, lambda: controller.handle('start_capture', {}))
    if args.compact:
        window_action('compact', True)
    elif controller.store.config['always_on_top']:
        window_action('always_on_top', True)
    if args.screenshot:
        def save_screen(ok):
            if not ok:
                app.exit(2)
                return
            def save():
                target = Path(args.screenshot)
                target.parent.mkdir(parents=True, exist_ok=True)
                success = view.grab().save(str(target))
                print('UI screenshot saved' if success else 'UI screenshot failed')
                app.exit(0 if success else 3)
            QTimer.singleShot(2000, save)
        view.loadFinished.connect(save_screen)
    if args.quit_after:
        QTimer.singleShot(args.quit_after * 1000, app.quit)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
