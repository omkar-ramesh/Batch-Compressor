"""Draws the app icon (a film frame being squeezed) and writes assets/icon.ico + icon.png."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "assets"
OUT.mkdir(exist_ok=True)

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded dark tile
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=96, fill=(27, 27, 30, 255))
# film frame
fx0, fy0, fx1, fy1 = 112, 136, S - 112, S - 136
d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=28, fill=(240, 181, 68, 255))
d.rounded_rectangle((fx0 + 26, fy0 + 26, fx1 - 26, fy1 - 26), radius=14, fill=(20, 20, 22, 255))
# sprocket holes
for y in range(fy0 + 40, fy1 - 30, 48):
    d.rounded_rectangle((fx0 - 44, y, fx0 - 14, y + 26), radius=6, fill=(235, 233, 228, 255))
    d.rounded_rectangle((fx1 + 14, y, fx1 + 44, y + 26), radius=6, fill=(235, 233, 228, 255))
# squeeze arrows
d.polygon([(44, S // 2 - 40), (44, S // 2 + 40), (fx0 - 60, S // 2)], fill=(134, 213, 138, 255))
d.polygon([(S - 44, S // 2 - 40), (S - 44, S // 2 + 40), (fx1 + 60, S // 2)], fill=(134, 213, 138, 255))
# tiny "photo" inside: mountain + sun
d.ellipse((fx0 + 70, fy0 + 70, fx0 + 118, fy0 + 118), fill=(240, 181, 68, 255))
d.polygon([(fx0 + 40, fy1 - 44), (fx0 + 150, fy0 + 150), (fx0 + 215, fy1 - 100), (fx0 + 255, fy1 - 140), (fx1 - 40, fy1 - 44)], fill=(134, 213, 138, 255))

img.save(OUT / "icon.png")
img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", OUT / "icon.ico")
