import cv2
import matplotlib.pyplot as plt
import numpy as np
from sklearn.cluster import KMeans

# ==========================================
# 設定色彩心情模式，可選：'Colorful', 'Bright', 'Muted', 'Deep'
# ==========================================
SELECTED_MOOD = 'Bright'

# 1. 讀取圖片並轉為 RGB
image_path = 'data/sample_5.jpg'
img_bgr = cv2.imread(image_path)

if img_bgr is None:
	print('無法讀取圖片，請檢查路徑！')
else:
	img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

	# 2. 影像縮放 (Resizing) - 設為寬 300 像素以加速運算
	target_width = 300
	height, width = img_rgb.shape[:2]
	target_height = int(height * (target_width / width))

	img_resized_rgb = cv2.resize(
		img_rgb, (target_width, target_height), interpolation=cv2.INTER_AREA
	)

	# 3. 計算視覺顯著性圖 (Saliency Map)，結合飽和度與邊緣細節
	gray = cv2.cvtColor(img_resized_rgb, cv2.COLOR_RGB2GRAY)
	hsv_full = cv2.cvtColor(img_resized_rgb, cv2.COLOR_RGB2HSV)
	s_channel = hsv_full[:, :, 1].astype(float) / 255.0

	laplacian = cv2.Laplacian(gray, cv2.CV_64F)
	edge_map = np.abs(laplacian)
	if edge_map.max() > 0:
		edge_map = edge_map / edge_map.max()

	# 以飽和度與高頻邊緣為主導，讓角落鮮豔色彩（如植物）也能被捕捉
	saliency_map = (s_channel * 0.6) + (edge_map * 0.4)
	saliency_map = np.clip(saliency_map, 0.1, 1.0)

	v_channel = hsv_full[:, :, 2]
	valid_mask = v_channel >= 30

	pixels = img_resized_rgb.reshape(-1, 3)
	weights = saliency_map.reshape(-1)

	valid_indices_flat = np.where(valid_mask.reshape(-1))[0]
	valid_pixels = pixels[valid_indices_flat]
	valid_weights = weights[valid_indices_flat]

	# ==========================================
	# 4. 階段一：透過帶權重的 K-Means 抓出 10 個「候選顯著色」
	# ==========================================
	candidate_k = 10
	kmeans = KMeans(n_clusters=candidate_k, random_state=42, n_init=10)
	labels = kmeans.fit_predict(valid_pixels, sample_weight=valid_weights)
	centers = kmeans.cluster_centers_

	candidate_colors = []
	candidate_scores = []

	for i in range(candidate_k):
		cluster_mask = labels == i
		cluster_pixels = valid_pixels[cluster_mask]
		cluster_weights = valid_weights[cluster_mask]

		if len(cluster_pixels) > 0:
			distances = np.linalg.norm(cluster_pixels - centers[i], axis=1)
			best_idx = np.argmin(distances - (cluster_weights * 50))
			candidate_colors.append(cluster_pixels[best_idx])
			candidate_scores.append(np.sum(cluster_weights))
		else:
			candidate_colors.append(centers[i])
			candidate_scores.append(0.0)

	candidate_colors = np.array(candidate_colors, dtype=int)
	candidate_scores = np.array(candidate_scores)

	# ==========================================
	# 5. 階段二：精準調校的 Mood 評分與過濾模型
	# ==========================================
	mood_scores = []
	for i in range(candidate_k):
		color = candidate_colors[i]
		base_score = candidate_scores[i]

		rgb_arr = np.uint8([[color]])
		hsv_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2HSV)
		s_val = hsv_arr[0, 0][1] / 255.0  # 飽和度 (0~1)
		v_val = hsv_arr[0, 0][2] / 255.0  # 明度 (0~1)

		if SELECTED_MOOD == 'Colorful':
			mood_factor = (s_val ** 3) * 4.0 + 0.1
		elif SELECTED_MOOD == 'Bright':
			# 明亮模式：強烈偏好高明度與適度飽和，壓抑暗部
			mood_factor = (v_val ** 2) * (s_val * 2.5 + 0.2) * 4.0
		elif SELECTED_MOOD == 'Muted':
			# 柔和模式 (莫蘭迪)：鎖定中低飽和度區間 (約 0.2 ~ 0.4)
			saturation_distance = abs(s_val - 0.3)
			mood_factor = max(0.05, 1.0 - (saturation_distance * 3.0))
		elif SELECTED_MOOD == 'Deep':
			# 深沉模式：強烈偏好低明度 (暗色系)
			mood_factor = ((1.0 - v_val) ** 1.2) * 4.0 + 0.1
		else:
			mood_factor = 1.0

		final_score = base_score * mood_factor
		mood_scores.append(final_score)

	top_5_indices = np.argsort(mood_scores)[::-1][:5]
	sorted_centers = candidate_colors[top_5_indices]

	total_score = np.sum(candidate_scores[top_5_indices])
	if total_score > 0:
		sorted_percentages = candidate_scores[top_5_indices] / total_score
	else:
		sorted_percentages = np.ones(5) / 5.0

	# 建立全圖對應遮罩供精準位置標註使用
	all_pixels = img_resized_rgb.reshape(-1, 3)
	distances_all = np.linalg.norm(
		all_pixels[:, np.newaxis, :] - sorted_centers[np.newaxis, :, :], axis=2
	)
	full_labels = np.argmin(distances_all, axis=1)
	label_mask_2d = full_labels.reshape(target_height, target_width)

	# 印出統計結果
	print(f'--- 色彩分佈統計與位置分析 [{SELECTED_MOOD} 模式] ---')
	for i in range(5):
		print(
			f'顏色 {i+1} | RGB: {tuple(sorted_centers[i])} | 最終權重佔比: {sorted_percentages[i]*100:.2f}%'
		)

	# ==========================================
	# 6. 視覺化呈現 (1 列 4 欄：顯著性圖、色相分布圖、精準定位圖、比例色條)
	# ==========================================
	fig, axes = plt.subplots(1, 4, figsize=(22, 6))

	# 圖 1：視覺顯著性熱力圖
	axes[0].imshow(saliency_map, cmap='inferno')
	axes[0].set_title(
		'Visual Saliency Map\n(Attention Weighting)',
		fontsize=10,
		fontweight='bold',
	)
	axes[0].axis('off')

	# 圖 2：色相分布光譜圖
	axes[1].set_title(
		'Hue Distribution Spectrum', fontsize=10, fontweight='bold'
	)
	axes[1].set_xlabel('Hue Angle (Degrees)', fontsize=9)
	axes[1].set_ylabel('Pixel Frequency', fontsize=9)

	h_channel = hsv_full[:, :, 0]
	bins_count = 36
	hist_vals, bin_edges = np.histogram(
		h_channel.ravel(), bins=bins_count, range=(0, 180)
	)
	bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])

	for count, center in zip(hist_vals, bin_centers):
		hue_deg = center * 2
		hsv_sample = np.uint8([[[center, 255, 255]]])
		rgb_sample = cv2.cvtColor(hsv_sample, cv2.COLOR_HSV2RGB)[0, 0] / 255.0
		axes[1].bar(
			hue_deg,
			count,
			width=360 / bins_count,
			color=rgb_sample,
			edgecolor='black',
			linewidth=0.3,
			alpha=0.9,
		)
	axes[1].set_xlim(0, 360)
	axes[1].grid(True, linestyle='--', alpha=0.4)

	# 圖 3：精準像素定位點圖
	axes[2].imshow(img_resized_rgb)
	axes[2].set_title(
		f'Salient Color Positions\n[{SELECTED_MOOD} Mode]',
		fontsize=10,
		fontweight='bold',
	)
	axes[2].axis('off')

	for i in range(5):
		y_indices, x_indices = np.where(label_mask_2d == i)
		if len(y_indices) > 0:
			cluster_pixels_rgb = img_resized_rgb[y_indices, x_indices]
			cluster_hsv = cv2.cvtColor(
				np.uint8([cluster_pixels_rgb]), cv2.COLOR_RGB2HSV
			)[0]
			v_vals = cluster_hsv[:, 2].astype(float) / 255.0
			color_diffs = np.linalg.norm(
				cluster_pixels_rgb - sorted_centers[i], axis=1
			)

			# 針對 Bright/Colorful 模式，在取樣時額外偏好較高明度或色彩飽和的真實像素
			if SELECTED_MOOD in ['Bright', 'Colorful']:
				selection_scores = color_diffs - (v_vals * 40)
			else:
				selection_scores = color_diffs

			best_idx = np.argmin(selection_scores)
			best_x = x_indices[best_idx]
			best_y = y_indices[best_idx]
			color_norm = sorted_centers[i] / 255.0

			axes[2].scatter(
				best_x,
				best_y,
				s=150,
				facecolors=color_norm,
				edgecolors='white',
				linewidths=2,
			)
			axes[2].text(
				best_x,
				best_y,
				str(i + 1),
				color='white',
				fontsize=9,
				fontweight='bold',
				ha='center',
				va='center',
			)

	# 圖 4：橫向比例色條
	ax4 = axes[3]
	current_width = 0
	for color, pct in zip(sorted_centers, sorted_percentages):
		color_normalized = color / 255.0
		ax4.barh(
			0,
			pct,
			left=current_width,
			color=color_normalized,
			edgecolor='none',
			height=0.6,
		)
		current_width += pct

	ax4.set_xlim(0, 1)
	ax4.set_ylim(-0.5, 0.5)
	ax4.axis('off')
	ax4.set_title(
		f'Extracted Colors [{SELECTED_MOOD}]', fontsize=10, fontweight='bold'
	)

	current_width = 0
	for i, (color, pct) in enumerate(zip(sorted_centers, sorted_percentages)):
		center_x = current_width + pct / 2
		rgb_tuple = tuple(color)
		ax4.text(
			center_x,
			-0.2,
			f'#{i+1}: {pct*100:.1f}%\nRGB{rgb_tuple}',
			ha='center',
			va='top',
			fontsize=8,
			bbox=dict(
				boxstyle='round,pad=0.3',
				facecolor='white',
				edgecolor='gray',
				alpha=0.8,
			),
		)
		current_width += pct

	plt.tight_layout()
	plt.show()