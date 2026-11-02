from PIL import Image
import numpy as np

img = Image.open("scripts/current_screen.png")
print("尺寸:", img.size)
arr = np.array(img)
print("平均颜色:", arr.mean(axis=(0, 1)))
print("是否有非黑内容:", (arr > 10).any())
