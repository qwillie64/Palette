// 在 Web Worker 中以 Pyodide 執行 color_cluster.py，避免運算時卡住頁面。
//
// 收到的訊息：{ id, count, mood, image? }
//   image 為圖片檔案內容 (ArrayBuffer)，只有在更換圖片時才需要附上
// 送出的訊息：
//   { type: 'progress', percent }      運算環境的載入進度 (0 ~ 100)
//   { type: 'ready' }                  運算環境載入完成
//   { type: 'result', id, colors }     擷取結果
//   { type: 'error', id, message }     單次擷取失敗
//   { type: 'fatal', message }         運算環境載入失敗

import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';

// 上傳的圖片在 Pyodide 虛擬檔案系統中的存放路徑
const IMAGE_PATH = '/tmp/upload';

// Pyodide v314.0.7 的核心與所需套件解壓後的總大小 (位元組)，更換版本時需一併更新
const DOWNLOAD_BYTES = 44_600_000;

// 下載階段在整體載入進度中所占的百分比，其餘為初始化 Python 模組的時間
const DOWNLOAD_PERCENT = 80;

// 下載完成後依序初始化的 Python 模組，以及各自完成時的進度百分比
const IMPORT_STEPS = [
	['numpy', 82],
	['scipy.sparse', 86],
	['sklearn', 96],
	['sklearn.cluster', 99],
];

let reportedPercent = -1;
let downloadedBytes = 0;

// 回報載入進度，進度只會前進不會後退
function reportProgress(percent) {
	percent = Math.floor(percent);
	if (percent > reportedPercent) {
		reportedPercent = percent;
		self.postMessage({ type: 'progress', percent });
	}
}

// 取代 fetch 以統計下載量。
// 帶有 integrity 的請求要等整個檔案下載並驗證完才會回應，過程中無法得知進度，
// 因此改為自行讀取資料流，下載完成後再自行驗證雜湊值。
const nativeFetch = self.fetch;

async function fetchWithProgress(input, init = {}) {
	const { integrity, ...options } = init;
	const response = await nativeFetch(input, options);
	if (!response.ok || !response.body) {
		return response;
	}

	const chunks = [];
	const reader = response.body.getReader();
	for (;;) {
		const { done, value } = await reader.read();
		if (done) {
			break;
		}
		chunks.push(value);
		downloadedBytes += value.length;
		reportProgress(
			Math.min(downloadedBytes / DOWNLOAD_BYTES, 1) * DOWNLOAD_PERCENT
		);
	}

	const body = await new Blob(chunks).arrayBuffer();
	if (integrity) {
		await verifyIntegrity(body, integrity, response.url);
	}
	return new Response(body, {
		status: response.status,
		statusText: response.statusText,
		headers: { 'Content-Type': response.headers.get('Content-Type') ?? '' },
	});
}

// 驗證下載內容的雜湊值，integrity 的格式為 "sha256-<base64 雜湊值>"
async function verifyIntegrity(body, integrity, url) {
	const separator = integrity.indexOf('-');
	const algorithm = integrity.slice(0, separator).replace('sha', 'SHA-');
	const digest = await crypto.subtle.digest(algorithm, body);
	const actual = btoa(String.fromCharCode(...new Uint8Array(digest)));
	if (actual !== integrity.slice(separator + 1)) {
		throw new Error(`檔案完整性驗證失敗：${url}`);
	}
}

const ready = (async () => {
	reportProgress(0);
	self.fetch = fetchWithProgress;
	let pyodide;
	try {
		pyodide = await loadPyodide();
		await pyodide.loadPackage(['opencv-python', 'scikit-learn']);
	} finally {
		self.fetch = nativeFetch;
	}
	reportProgress(DOWNLOAD_PERCENT);

	for (const [module, percent] of IMPORT_STEPS) {
		pyodide.pyimport(module).destroy();
		reportProgress(percent);
	}

	const response = await fetch('color_cluster.py');
	if (!response.ok) {
		throw new Error(`無法載入 color_cluster.py (HTTP ${response.status})`);
	}
	pyodide.FS.writeFile('color_cluster.py', await response.text());

	const extractColors = pyodide.pyimport('color_cluster').extract_colors;
	reportProgress(100);
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
