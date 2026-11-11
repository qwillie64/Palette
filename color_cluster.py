import os

import cv2
import numpy as np
from sklearn.cluster import KMeans

# 可選的色彩心情模式
MOODS = ('Colorful', 'Bright', 'Muted', 'Deep')

# 可擷取的顏色數量上限
MAX_COLORS = 8

# 第一階段 K-Means 產生的候選色數量，需大於等於 MAX_COLORS
CANDIDATE_K = 10

# 運算前將圖片縮放到的寬度 (像素)，用來加速運算
TARGET_WIDTH = 300


def extract_colors(image, n_colors=5, mood='Bright'):
	"""從圖片擷取顯著顏色。

	參數：
		image: 圖片檔案路徑，或圖片檔案的原始位元組內容 (bytes)
		n_colors: 要擷取的顏色數量，範圍 0 ~ MAX_COLORS
		mood: 色彩心情模式，可選值見 MOODS

	回傳：
		依心情評分由高到低排序的 list，每個元素為 dict：
			rgb: (r, g, b)，各分量 0 ~ 255
			hex: '#rrggbb' 格式的色碼
			ratio: 該顏色在擷取結果中的權重佔比 (0 ~ 1)
	"""
	if mood not in MOODS:
		raise ValueError(f'未知的 mood：{mood!r}，可選值為 {MOODS}')
	if not 0 <= n_colors <= MAX_COLORS:
		raise ValueError(f'n_colors 必須介於 0 ~ {MAX_COLORS}')

	# 1. 解碼圖片並轉為 RGB
	#    路徑先讀成位元組再解碼，避免 cv2.imread 無法處理非 ASCII 路徑
	if isinstance(image, (str, os.PathLike)):
		data = np.fromfile(image, dtype=np.uint8)
	else:
		data = np.frombuffer(image, dtype=np.uint8)
	img_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
	if img_bgr is None:
		raise ValueError('無法讀取圖片，請確認檔案為支援的圖片格式')
	if n_colors == 0:
		return []
	img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

	# 2. 等比例縮放
	height, width = img_rgb.shape[:2]
	target_height = max(1, int(height * TARGET_WIDTH / width))
	img_rgb = cv2.resize(
		img_rgb, (TARGET_WIDTH, target_height), interpolation=cv2.INTER_AREA
	)

	# 3. 計算視覺顯著性作為每個像素的權重
	#    以飽和度為主、邊緣細節為輔，讓面積小但鮮豔的區域也能被捕捉
	gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
	hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
	saturation = hsv[:, :, 1] / 255.0

	edge_map = np.abs(cv2.Laplacian(gray, cv2.CV_64F))
	if edge_map.max() > 0:
		edge_map /= edge_map.max()

	saliency_map = np.clip(saturation * 0.6 + edge_map * 0.4, 0.1, 1.0)

	# 排除過暗的像素 (明度低於 30)；若剩餘像素不足以分群則使用全部像素
	pixels = img_rgb.reshape(-1, 3)
	weights = saliency_map.reshape(-1)
	valid_mask = hsv[:, :, 2].reshape(-1) >= 30
	if valid_mask.sum() >= CANDIDATE_K:
		pixels = pixels[valid_mask]
		weights = weights[valid_mask]

	# 4. 階段一：以帶權重的 K-Means 找出候選色
	#    每個群集取「接近中心且顯著性高」的真實像素作為代表色，
	#    並以群集內的權重總和作為該候選色的基礎分數
	kmeans = KMeans(
		n_clusters=min(CANDIDATE_K, len(pixels)), random_state=42, n_init=10
	)
	labels = kmeans.fit_predict(pixels, sample_weight=weights)

	candidate_colors = []
	candidate_scores = []
	for i, center in enumerate(kmeans.cluster_centers_):
		cluster_mask = labels == i
		if not cluster_mask.any():
			continue
		cluster_pixels = pixels[cluster_mask]
		cluster_weights = weights[cluster_mask]
		distances = np.linalg.norm(cluster_pixels - center, axis=1)
		best_idx = np.argmin(distances - cluster_weights * 50)
		candidate_colors.append(cluster_pixels[best_idx])
		candidate_scores.append(cluster_weights.sum())

	candidate_colors = np.array(candidate_colors, dtype=np.uint8)
	candidate_scores = np.array(candidate_scores)

	# 5. 階段二：依 mood 對候選色加權評分，取分數最高的 n_colors 個
	candidate_hsv = cv2.cvtColor(
		candidate_colors[np.newaxis], cv2.COLOR_RGB2HSV
	)[0]
	s = candidate_hsv[:, 1] / 255.0
	v = candidate_hsv[:, 2] / 255.0

	if mood == 'Colorful':
		# 鮮豔：強烈偏好高飽和度
		mood_factor = s**3 * 4.0 + 0.1
	elif mood == 'Bright':
		# 明亮：偏好高明度與適度飽和，壓抑暗部
		mood_factor = v**2 * (s * 2.5 + 0.2) * 4.0
	elif mood == 'Muted':
		# 柔和 (莫蘭迪色)：偏好飽和度接近 0.3 的中低飽和區間
		mood_factor = np.maximum(0.05, 1.0 - np.abs(s - 0.3) * 3.0)
	else:
		# 深沉：偏好低明度的暗色系
		mood_factor = (1.0 - v) ** 1.2 * 4.0 + 0.1

	top_indices = np.argsort(candidate_scores * mood_factor)[::-1][:n_colors]
	top_colors = candidate_colors[top_indices]
	top_ratios = candidate_scores[top_indices] / candidate_scores[top_indices].sum()

	return [
		{
			'rgb': (r, g, b),
			'hex': f'#{r:02x}{g:02x}{b:02x}',
			'ratio': ratio,
		}
		for (r, g, b), ratio in zip(top_colors.tolist(), top_ratios.tolist())
	]


if __name__ == '__main__':
	import sys

	# 用法：python color_cluster.py <圖片路徑> [顏色數量] [mood]
	path = sys.argv[1] if len(sys.argv) > 1 else 'data/sample_5.jpg'
	count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
	selected_mood = sys.argv[3] if len(sys.argv) > 3 else 'Bright'

	print(f'--- 擷取結果 [{selected_mood} 模式] ---')
	for index, color in enumerate(extract_colors(path, count, selected_mood), 1):
		print(
			f"顏色 {index} | {color['hex']} | RGB: {color['rgb']} | "
			f"權重佔比: {color['ratio'] * 100:.2f}%"
		)
