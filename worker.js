// 在 Web Worker 中以 Pyodide 執行 color_cluster.py，避免運算時卡住頁面。
//
// 收到的訊息：{ id, count, mood, image? }
//   image 為圖片檔案內容 (ArrayBuffer)，只有在更換圖片時才需要附上
// 送出的訊息：
//   { type: 'ready' }                  運算環境載入完成
//   { type: 'result', id, colors }     擷取結果
//   { type: 'error', id, message }     單次擷取失敗
//   { type: 'fatal', message }         運算環境載入失敗

import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';

// 上傳的圖片在 Pyodide 虛擬檔案系統中的存放路徑
const IMAGE_PATH = '/tmp/upload';

const ready = (async () => {
	const pyodide = await loadPyodide();
	await pyodide.loadPackage(['opencv-python', 'scikit-learn']);

	const response = await fetch('color_cluster.py');
	if (!response.ok) {
		throw new Error(`無法載入 color_cluster.py (HTTP ${response.status})`);
	}
	pyodide.FS.writeFile('color_cluster.py', await response.text());

	const extractColors = pyodide.pyimport('color_cluster').extract_colors;
	self.postMessage({ type: 'ready' });
	return { pyodide, extractColors };
})();

let latestRequest = null;
let pendingImage = null;
let running = false;

self.onmessage = (event) => {
	const { image, ...request } = event.data;
	if (image) {
		pendingImage = image;
	}
	latestRequest = request;
	if (!running) {
		processRequests();
	}
};

async function processRequests() {
	running = true;
	let runtime;
	try {
		runtime = await ready;
	} catch (error) {
		self.postMessage({ type: 'fatal', message: error.message });
		return;
	}
	const { pyodide, extractColors } = runtime;

	// 運算期間可能累積多個請求，只處理最新的一個
	while (latestRequest) {
		const { id, count, mood } = latestRequest;
		latestRequest = null;
		try {
			if (pendingImage) {
				pyodide.FS.writeFile(IMAGE_PATH, new Uint8Array(pendingImage));
				pendingImage = null;
			}
			const result = extractColors(IMAGE_PATH, count, mood);
			const colors = result.toJs({ dict_converter: Object.fromEntries });
			result.destroy();
			self.postMessage({ type: 'result', id, colors });
		} catch (error) {
			// Python 例外的訊息包含完整 traceback，只取最後一行的錯誤說明
			const lastLine = error.message.trim().split('\n').pop();
			self.postMessage({
				type: 'error',
				id,
				message: lastLine.replace(/^\w+Error:\s*/, ''),
			});
		}
		// 讓出執行權，使排隊中的訊息能先更新 latestRequest
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	running = false;
}
