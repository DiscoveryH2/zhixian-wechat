from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
im = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((8, 8, 248, 248), radius=56, fill='#172525')
d.rounded_rectangle((62, 80, 81, 177), radius=9, fill='#83ddbe')
d.rounded_rectangle((99, 53, 118, 204), radius=9, fill='#83ddbe')
d.rounded_rectangle((136, 94, 155, 164), radius=9, fill='#c6f3e3')
d.rounded_rectangle((173, 72, 192, 186), radius=9, fill='#83ddbe')
im.save(root / 'ui/icon.png')
im.save(root / 'ui/icon.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
