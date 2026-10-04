"""System speech synthesis; text stays on this device."""
from PySide6.QtTextToSpeech import QTextToSpeech


class Speech:
    def __init__(self):
        self.engine = QTextToSpeech()

    def say(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('朗读文字需要在 1–2000 字之间。')
        if not self.engine.availableVoices():
            raise ValueError('系统暂无可用语音，请在系统设置中安装语音。')
        self.engine.stop()
        self.engine.say(text)
        return {'success': True, 'message': '正在使用系统语音朗读。'}

    def stop(self):
        self.engine.stop()
