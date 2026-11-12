# Palette 顏色擷取：Flutter 實作開發說明

本文件說明如何把目前網頁版（`index.html` + `worker.js` + `color_cluster.py`）的「圖片顏色擷取」功能移植為 Flutter 功能。目標是讓開發人員不需要讀原始 Python 程式碼，只看這份文件就能完整實作，並驗證結果與現行版本一致。

---

## 1. 目標與範圍

### 1.1 要做的事
- 以 **純 Dart** 重新實作 `color_cluster.py` 的 `extract_colors` 演算法，不依賴 Python、OpenCV、scikit-learn。
- 重現網頁版的 UI 與互動：上傳圖片、調整顏色數量（0 ~ 8）、切換 Mood、顯示色塊、點擊複製色碼。
- 運算放在背景 Isolate 執行，不能卡住 UI。

### 1.2 不做的事
- `note.md` 提到的診斷圖表（顯著性熱力圖、色相光譜、取樣點標記）目前網頁版沒有做，這次也不做。
- Pyodide 的載入進度條。改成純 Dart 之後不需要下載運算環境，這個狀態直接移除。

### 1.3 現行架構與 Flutter 架構對照

| 現行（Web） | Flutter |
|---|---|
| `color_cluster.py`（OpenCV + scikit-learn） | `lib/palette/` 底下的純 Dart 演算法 |
| `worker.js`（Web Worker + Pyodide） | `Isolate.run()` |
| `index.html` 的 UI 與狀態 | `PaletteController`（ChangeNotifier）+ Widgets |
| 每次調整數量或 Mood 都重跑整個演算法 | **只有換圖片時才跑重運算**，數量和 Mood 的變化在主執行緒即時計算（見 §3.2） |

---

## 2. 對外介面（資料模型）

```dart
/// 色彩心情模式。name 要和 Python 的字串一致：Colorful / Bright / Muted / Deep
enum ColorMood { colorful, bright, muted, deep }

/// 演算法第一階段的產出：候選色（最多 10 個）
class ColorCandidate {
  final int r, g, b;     // 0 ~ 255，取自圖片中真實存在的像素
  final double score;    // 群集內顯著性權重的總和（原始分數，不含 mood 加權）
  const ColorCandidate(this.r, this.g, this.b, this.score);
}

/// 最終結果中的一個顏色
class PaletteColor {
  final int r, g, b;
  final double ratio;    // 在本次結果中的權重佔比，所有 ratio 加總為 1
  const PaletteColor(this.r, this.g, this.b, this.ratio);

  /// 小寫 '#rrggbb'，和 Python 的輸出一致（UI 顯示時才轉大寫）
  String get hex =>
      '#${[r, g, b].map((c) => c.toRadixString(16).padLeft(2, '0')).join()}';
}

/// 重運算：解碼圖片並產生候選色。在 Isolate 中執行。
/// 圖片無法解碼時丟出 [ImageDecodeException]。
List<ColorCandidate> analyzeImage(Uint8List imageBytes);

/// 輕量運算：依 mood 從候選色挑出 n 個顏色。在主執行緒執行即可。
/// n 不在 0 ~ 8 時丟出 RangeError。
List<PaletteColor> selectPalette(List<ColorCandidate> candidates, int n, ColorMood mood);

/// 方便呼叫的組合版本，等同 Python 的 extract_colors(image, n_colors, mood)
List<PaletteColor> extractColors(Uint8List imageBytes, {int n = 5, ColorMood mood = ColorMood.bright}) =>
    selectPalette(analyzeImage(imageBytes), n, mood);
```

### 常數（必須和 Python 一致）

| 名稱 | 值 | 用途 |
|---|---|---|
| `kMaxColors` | 8 | 顏色數量上限 |
| `kCandidateK` | 10 | 第一階段 K-Means 的群數 |
| `kTargetWidth` | 300 | 運算前縮放的寬度（px） |
| `kMinValue` | 30 | 明度低於此值的像素視為過暗 |
| `kSaliencyFloor` / `kSaliencyCeil` | 0.1 / 1.0 | 顯著性權重的範圍 |
| `kAnchorWeight` | 50 | 選代表色時顯著性的加權係數 |
| K-Means 參數 | `n_init = 10`、`max_iter = 300`、`tol = 1e-4`、`seed = 42` | 對齊 scikit-learn 預設 |

---

## 3. 演算法規格

### 3.1 流程總覽

```
圖片位元組
  │ ① 解碼 + EXIF 轉正 + 轉成 8-bit RGB
  │ ② INTER_AREA 縮放到寬 300
  │ ③ 計算灰階、HSV（S、V）、Laplacian 邊緣 → 顯著性權重
  │ ④ 濾掉過暗像素（V < 30）
  │ ⑤ 帶權重 K-Means（k = 10）
  │ ⑥ 每群取一個真實像素當代表色，分數 = 群內權重總和
  ▼
候選色（≤ 10 個）               ← analyzeImage，只跟圖片有關
  │ ⑦ 分數 × mood 係數，取前 n 名
  │ ⑧ ratio = 原始分數 / 入選者原始分數總和
  ▼
PaletteColor 列表               ← selectPalette，跟 n、mood 有關
```

### 3.2 拆成兩階段的理由
Python 版每次都從頭跑，但 ①～⑥ **完全不受 `n_colors` 和 `mood` 影響**。所以 Flutter 版每張圖片只做一次 `analyzeImage` 並快取結果，之後調整數量或 Mood 只需要呼叫 `selectPalette`（10 個元素的排序，幾乎不花時間）。輸出和 Python 完全一樣，但互動是即時的，也不需要網頁版那種 200ms debounce 和「只處理最新請求」的機制。

### 3.3 步驟 ①：解碼

使用 [`image`](https://pub.dev/packages/image) 套件（4.x）：

```dart
import 'package:image/image.dart' as img;

({Uint8List rgb, int width, int height}) decodeRgb(Uint8List bytes) {
  final decoded = bytes.isEmpty ? null : img.decodeImage(bytes);
  if (decoded == null) {
    throw const ImageDecodeException('無法讀取圖片，請確認檔案為支援的圖片格式');
  }
  // OpenCV 的 IMREAD_COLOR 會依 EXIF 旋轉圖片，這裡要做一樣的事
  final oriented = img.bakeOrientation(decoded);
  // 對應 IMREAD_COLOR：轉為 8-bit、捨棄 alpha（保留透明區域底下原本的 RGB 值，不與背景混色）
  final rgb8 = oriented.convert(format: img.Format.uint8, numChannels: 3);
  return (
    rgb: rgb8.getBytes(order: img.ChannelOrder.rgb),
    width: rgb8.width,
    height: rgb8.height,
  );
}
```

注意事項：
- 支援格式要涵蓋 JPEG、PNG、WebP、BMP（網頁版 `accept` 的四種）。
- **動畫 GIF / WebP**：只取第一張影格（`decodeImage` 預設行為）。
- **iOS 的 HEIC**：`image` 套件不支援。從相簿挑選時請 `image_picker` 回傳 JPEG（見 §5.4）；如果還是收到 HEIC，退回用 `dart:ui` 的 `instantiateImageCodec` 解碼後取 `rawRgba`（必須在主 Isolate 做，再把 RGBA 位元組傳進背景 Isolate）。
- 透明 PNG 要寫測試確認 `convert(numChannels: 3)` 保留的是原始 RGB，沒有跟黑或白混色。

### 3.4 步驟 ②：縮放（INTER_AREA）

```
targetWidth  = 300
targetHeight = max(1, floor(height * 300 / width))   // Python 的 int() 是無條件捨去
```

寬度小於 300 時也要**放大**到 300（Python 版會這樣做）。

依面積加權平均重新取樣（可分離成水平、垂直兩次），中間用浮點數計算，最後四捨五入並限制在 0 ~ 255：

```dart
/// 每個目的像素對應到的來源像素與權重（權重總和為 1）
List<List<(int, double)>> _areaWeights(int src, int dst) {
  final scale = src / dst;
  return List.generate(dst, (d) {
    final start = d * scale, end = (d + 1) * scale;
    final out = <(int, double)>[];
    for (var s = start.floor(); s < end.ceil() && s < src; s++) {
      final overlap = math.min(end, s + 1.0) - math.max(start, s.toDouble());
      if (overlap > 1e-9) out.add((s, overlap / scale));
    }
    return out;
  });
}

Uint8List resizeArea(Uint8List rgb, int sw, int sh, int dw, int dh) {
  final wx = _areaWeights(sw, dw), wy = _areaWeights(sh, dh);
  final tmp = Float64List(sh * dw * 3);              // 水平縮放後的結果
  for (var y = 0; y < sh; y++) {
    for (var dx = 0; dx < dw; dx++) {
      for (final (s, w) in wx[dx]) {
        for (var c = 0; c < 3; c++) {
          tmp[(y * dw + dx) * 3 + c] += rgb[(y * sw + s) * 3 + c] * w;
        }
      }
    }
  }
  final out = Uint8List(dw * dh * 3);
  for (var dy = 0; dy < dh; dy++) {
    for (var dx = 0; dx < dw; dx++) {
      for (var c = 0; c < 3; c++) {
        var v = 0.0;
        for (final (s, w) in wy[dy]) v += tmp[(s * dw + dx) * 3 + c] * w;
        out[(dy * dw + dx) * 3 + c] = v.round().clamp(0, 255);
      }
    }
  }
  return out;
}
```

> 縮小時這個做法和 OpenCV 的 INTER_AREA 一樣。放大時 OpenCV 用的是另一套近似線性的公式，會有 ±1 左右的差異，對結果影響可忽略。

### 3.5 步驟 ③：灰階、HSV、邊緣、顯著性

以下公式都是 OpenCV 8-bit 的**定點數運算**，照著做才能讓 S、V 的值和 Python 一模一樣（步驟 ⑦ 的 mood 評分會用到）。

**灰階**（`COLOR_RGB2GRAY`）：
```dart
int gray(int r, int g, int b) => (r * 4899 + g * 9617 + b * 1868 + 8192) >> 14;
```

**HSV 的 S 與 V**（`COLOR_RGB2HSV`，H 用不到不必算）：
```dart
// sdiv[v] = round((255 << 12) / v)，v = 0 時為 0。預先建表。
final _sdiv = List<int>.generate(256, (v) => v == 0 ? 0 : ((255 << 12) / v).round());

int hsvV(int r, int g, int b) => math.max(r, math.max(g, b));
int hsvS(int r, int g, int b) {
  final v = hsvV(r, g, b);
  final diff = v - math.min(r, math.min(g, b));
  return (diff * _sdiv[v] + (1 << 11)) >> 12;
}
```

**邊緣**（`Laplacian(gray, CV_64F)`，ksize = 1）：
- 卷積核 `[[0,1,0],[1,-4,1],[0,1,0]]`，結果取絕對值。
- 邊界採 **BORDER_REFLECT_101**：索引 −1 對應 1，索引 `n` 對應 `n-2`；若該方向長度只有 1，鄰居就是自己。
- 除以整張圖的最大值做正規化；最大值為 0（整張圖完全平坦）時維持全 0。

```dart
int reflect101(int i, int n) {
  if (n == 1) return 0;
  if (i < 0) return -i;
  if (i >= n) return 2 * n - 2 - i;
  return i;
}
// edge[y][x] = |g(y-1,x) + g(y+1,x) + g(y,x-1) + g(y,x+1) - 4*g(y,x)|
```

**顯著性權重**（每個像素一個）：
```
saliency = clamp(S/255 * 0.6 + edge * 0.4, 0.1, 1.0)
```

### 3.6 步驟 ④：過濾過暗像素

- 像素依 row-major 順序（由左到右、由上到下）攤平成列表，每個像素帶有 RGB 與 saliency 權重。
- 保留 `V >= 30` 的像素。
- **只有在保留下來的像素數 ≥ 10 時才套用過濾**，否則使用全部像素。

### 3.7 步驟 ⑤：帶權重 K-Means

需要自己實作，行為對齊 scikit-learn 的 `KMeans(n_clusters=min(10, 像素數), random_state=42, n_init=10)` 配合 `sample_weight`（Lloyd 演算法、k-means++ 初始化）。

#### 3.7.1 效能優化：合併重複顏色（建議實作）
300 px 寬的照片約有 6 ~ 9 萬個像素，其中很多顏色重複。把 RGB 完全相同的像素合併成一個點、權重相加之後再分群，在數學上和原本等價：加權平均、inertia、k-means++ 的抽樣機率都一樣。

- 以 `key = r << 16 | g << 8 | b` 建立 `Map<int, int>`（顏色 → 唯一點索引），累加權重。
- 分群完成後，每個原始像素的群標籤 = 它的顏色所屬唯一點的標籤。
- **步驟 ⑥ 仍然要用原始像素和各自的權重**，因為選代表色的公式用的是單一像素的權重。
- 唯一顏色數 ≤ k 時，直接讓每個唯一顏色自成一群，跳過 K-Means（等同 sklearn 在這種情況下的結果）。

#### 3.7.2 演算法
以下 `X` 為點座標（RGB，`Float64List`，長度 `n*3`），`w` 為權重，`d²` 為歐氏距離平方。

**tolerance**：`tolAbs = 1e-4 × (R、G、B 三個通道各自變異數的平均)`。變異數用**未加權**、步驟 ④ 之後的原始像素計算（除以 N）。

**k-means++ 初始化**（每次 init 執行一次）：
```
nLocalTrials = 2 + floor(ln(k))
c0 = 依機率 w / Σw 抽一個點
closest[i] = d²(X[i], c0)
pot = Σ w[i] * closest[i]
重複直到有 k 個中心：
    產生 nLocalTrials 個亂數 u ∈ [0, 1)，各乘上 pot
    cum = (w * closest) 的累積和
    每個亂數找第一個 cum[j] >= 值的 j（二分搜尋，結果上限 n-1）當候選點
    對每個候選點 t：newClosest_t[i] = min(closest[i], d²(X[i], X[t]))
                    pot_t = Σ w[i] * newClosest_t[i]
    選 pot_t 最小的候選點加入中心；closest = newClosest_t；pot = pot_t
```

**Lloyd 迭代**（最多 300 次）：
```
重複：
    labels[i] = 離 X[i] 最近的中心（距離相同取索引小的）
    新中心 = 各群的加權平均 Σ(w·X) / Σw
    空群處理：取「離自己目前中心最遠」的點，讓空群的中心移到那個點
            （有多個空群就依序取最遠、次遠……）
    shift = Σ_j ||新中心_j − 舊中心_j||²
    labels 和上一輪完全相同 → 收斂，結束
    shift <= tolAbs → 收斂，結束
結束後用最終中心重新計算一次 labels
inertia = Σ w[i] * d²(X[i], 中心[labels[i]])
```

**多次初始化**：跑 10 次，取 inertia 最小的那一次的中心與 labels。

**亂數**：使用 `Random(42)` 產生 10 個子種子（例如 `nextInt(1 << 32)`），每次 init 用自己的 `Random(子種子)`。這保證 Dart 版本身是確定性的（同一張圖每次結果相同），但**無法和 numpy 的亂數序列一致**，所以和 Python 的結果可能有小差異，驗收標準見 §6。

實作提示：所有座標與距離都用 `Float64List`／`Int32List`，內層迴圈避免配置物件。k = 10、3 維，展開成純迴圈即可，不需要 Elkan 等加速法。

### 3.8 步驟 ⑥：候選色

依群索引 `0..k-1` 的順序處理每一群（群內沒有任何像素就跳過）：

```
對群 i 內的每個原始像素 p（依 row-major 順序）：
    dist = sqrt(Σ (p.rgb − center_i)²)          // 注意是距離，不是平方
    key  = dist − p.weight * 50
代表色 = key 最小的像素（相同時取先出現的）
分數   = 群內所有像素的 weight 總和
```

產出依群索引排序的 `List<ColorCandidate>`（長度 ≤ 10）。

### 3.9 步驟 ⑦～⑧：selectPalette

```dart
double moodFactor(ColorCandidate c, ColorMood mood) {
  final s = hsvS(c.r, c.g, c.b) / 255.0;   // 一定要用 §3.5 的定點數 HSV
  final v = hsvV(c.r, c.g, c.b) / 255.0;
  switch (mood) {
    case ColorMood.colorful: return math.pow(s, 3) * 4.0 + 0.1;
    case ColorMood.bright:   return v * v * (s * 2.5 + 0.2) * 4.0;
    case ColorMood.muted:    return math.max(0.05, 1.0 - (s - 0.3).abs() * 3.0);
    case ColorMood.deep:     return math.pow(1.0 - v, 1.2) * 4.0 + 0.1;
  }
}

List<PaletteColor> selectPalette(List<ColorCandidate> candidates, int n, ColorMood mood) {
  RangeError.checkValueInInterval(n, 0, kMaxColors, 'n');
  if (n == 0) return const [];
  final order = List<int>.generate(candidates.length, (i) => i)
    ..sort((a, b) {
      final fa = candidates[a].score * moodFactor(candidates[a], mood);
      final fb = candidates[b].score * moodFactor(candidates[b], mood);
      final cmp = fb.compareTo(fa);       // 由高到低
      return cmp != 0 ? cmp : b.compareTo(a); // 同分時索引大的在前（對應 argsort 反轉）
    });
  final top = order.take(n).map((i) => candidates[i]).toList();
  // ratio 用「原始分數」計算，不是乘上 mood 係數後的分數
  final total = top.fold<double>(0, (sum, c) => sum + c.score);
  return [for (final c in top) PaletteColor(c.r, c.g, c.b, c.score / total)];
}
```

各 mood 的設計意圖：
- **Colorful**：強烈偏好高飽和度。
- **Bright**：偏好高明度且適度飽和，壓抑暗部。
- **Muted**（莫蘭迪色）：偏好飽和度接近 0.3。注意 `note.md` 寫的是高斯鐘形，但**程式實際用的是三角形函數**，以程式為準。
- **Deep**：偏好低明度的暗色。

### 3.10 邊界情況

| 情況 | 行為 |
|---|---|
| 空檔案或無法解碼 | 丟 `ImageDecodeException('無法讀取圖片，請確認檔案為支援的圖片格式')` |
| `n = 0` | 回傳空列表（圖片仍會先解碼，壞檔一樣報錯） |
| 候選色少於 `n`（例如單色圖只有 1 個候選色） | 回傳全部候選色，數量少於 n 是正常結果 |
| 幾乎全黑的圖（亮像素 < 10） | 不過濾，使用全部像素 |
| 極窄或極扁的圖 | `targetHeight` 最小為 1 |
| 寬度 < 300 | 放大到 300 |

---

## 4. 背景執行

```dart
final candidates = await Isolate.run(() => analyzeImage(bytes));
```

- `analyzeImage` 必須是頂層函式或 static 函式，而且不能用到任何 Flutter／UI 物件。
- `ImageDecodeException` 會從 Isolate 傳回並在 `await` 處丟出，要捕捉並顯示錯誤訊息。
- **效能目標**：一張 12MP 的手機照片，在中階 Android 手機的 release build 上，從選完圖到出現色塊 ≤ 1.5 秒。解碼大圖通常是最慢的一步，若超標可以改用 `dart:ui` 先解碼成縮小尺寸（`instantiateImageCodec` 的 `targetWidth` 設為原寬和 600 的較小值），再交給演算法處理。這會稍微改變縮放結果，需要重新跑 §6 的驗收。
- **Flutter Web**：Web 不支援 Isolate，`compute()` 會在主執行緒同步執行，運算期間 UI 會卡住。如果要發布 Web 版，需要另外處理（例如維持現有的網頁版）。

---

## 5. UI / UX 規格

### 5.1 版面

```
┌─────────────────────────────────────────────────────────┐
│ Palette          [上傳圖片]  顏色數量 [−] 5 [+]  Mood [Bright ▾] │  ← Header（Wrap，可換行）
├─────────────────────────────────────────────────────────┤
│ 狀態文字                                                 │
│ ┌──────────────────────┐  ┌──────────────────────┐      │
│ │                      │  │ #E4A23F   RGB(...)/32.1% │
│ │      圖片預覽         │  │ #2B5D7A   RGB(...)/24.0% │
│ │   (contain, 點擊上傳) │  │ ...                   │
│ └──────────────────────┘  └──────────────────────┘      │
└─────────────────────────────────────────────────────────┘
```

- **寬度 > 720**：左右兩欄等寬、間距 16，兩個面板填滿剩下的高度。
- **寬度 ≤ 720**：上下排列、整頁可捲動；面板最小高度 280，圖片最大高度為視窗高度的 60%。
- 外距：Header `12 × 20`、下方有 1px 分隔線；主區塊 `10 20 20`。
- 面板：圓角 12、1px 邊框、背景 `panel`，內容超出時可捲動。

### 5.2 色彩 Token（跟隨系統深淺色）

| Token | Light | Dark |
|---|---|---|
| bg | `#f4f4f2` | `#151515` |
| panel | `#ffffff` | `#1e1e1e` |
| border | `#dcdcd8` | `#353535` |
| text | `#1f1f1d` | `#ececea` |
| muted | `#6f6f6a` | `#9c9c96` |
| control | `#ffffff` | `#262626` |
| controlHover | `#ecece8` | `#323232` |
| error | `#b3261e` | `#f2b8b5` |

建議以 `ThemeExtension<PaletteColors>` 實作，透過 `MediaQuery.platformBrightnessOf` 決定使用哪一組。

### 5.3 元件

**Header**
- 標題「Palette」：18px、w600，靠左並佔滿剩餘空間。
- 按鈕／下拉選單：高 34、圓角 8、1px 邊框、背景 `control`，hover 時改為 `controlHover`（桌面版）。
- **顏色數量 Stepper**：`−`、數字、`+`。範圍 0 ~ 8，預設 5；到達上下限時對應按鈕停用（opacity 0.4）。數字寬 36、置中、w600、等寬數字（`FontFeature.tabularFigures()`）。
- **Mood 下拉選單**：Colorful / Bright / Muted / Deep，預設 Bright。

**狀態列**：13px、顏色 `muted`，錯誤時改用 `error`。最小高度 20，不會因為沒有文字而跳動。

**圖片面板**
- 沒有圖片時置中顯示「點擊或將圖片拖曳到這裡」（`muted`）。
- 有圖片時用 `Image.memory(fit: BoxFit.contain)` 顯示。
- 點擊面板任何位置都會開啟選圖。
- 桌面版支援拖曳：拖曳進入時邊框改為虛線、顏色 `text`；只接受第一個檔案。

**色塊列表**
- 沒有結果時顯示「擷取出的顏色會顯示在這裡」；有圖片但數量為 0 時顯示「顏色數量為 0」。
- 每個色塊垂直平均分配面板高度（`Expanded`），最小高度 44，左右內距 18。
- 背景為該顏色；文字顏色依亮度決定：`0.299r + 0.587g + 0.114b > 150` 時用 `#1f1f1d`，否則用白色。
- 左側：色碼**大寫**，16px、w600、等寬字型。
- 右側：`RGB(r, g, b) / 32.1%`（ratio × 100，小數點後一位），13px、等寬字型。
- 點擊：把**小寫**色碼（`PaletteColor.hex`）複製到剪貼簿（`Clipboard.setData`），左側文字改成「已複製」1 秒後恢復；失敗時顯示「無法複製」。
- 分析新圖片的期間，舊的色塊以 opacity 0.5 顯示（150ms 淡入淡出）。

### 5.4 選圖來源

| 平台 | 方式 | 套件（建議） |
|---|---|---|
| Android / iOS | 相簿（必要）、相機（選配） | `image_picker` |
| Windows / macOS / Linux | 檔案對話框 + 拖曳 | `file_picker`、`desktop_drop` |

- 只接受 JPEG / PNG / WebP / BMP；其他檔案顯示錯誤「請選擇圖片檔案」。
- iOS 使用 `image_picker` 時要確保拿到 JPEG 而不是 HEIC（例如指定 `imageQuality`，實際行為請在真機上驗證），否則依 §3.3 的備援方式解碼。
- 使用者重新選同一個檔案時也要重新分析。
- 加入套件前請到 pub.dev 確認是官方或廣泛使用的版本，並在 `pubspec.yaml` 鎖定版本。

### 5.5 狀態與文字

| 狀態 | 狀態列文字 |
|---|---|
| 尚未選圖 | 請上傳圖片 |
| 分析中 | 擷取中... |
| 完成，數量 > 0 | 點擊色塊可複製色碼 |
| 完成，數量 = 0 | （空白） |
| 解碼失敗 | 無法讀取圖片，請確認檔案為支援的圖片格式（紅字，並清空色塊） |
| 選到非圖片檔 | 請選擇圖片檔案（紅字） |

### 5.6 Controller

```dart
class PaletteController extends ChangeNotifier {
  Uint8List? imageBytes;
  List<ColorCandidate>? candidates;   // null = 尚未分析完成
  int count = 5;
  ColorMood mood = ColorMood.bright;
  bool busy = false;
  String? error;
  int _token = 0;                     // 用來丟棄過期的分析結果

  List<PaletteColor> get palette =>
      candidates == null ? const [] : selectPalette(candidates!, count, mood);

  Future<void> loadImage(Uint8List bytes) async {
    final token = ++_token;
    imageBytes = bytes; busy = true; error = null;
    notifyListeners();
    try {
      final result = await Isolate.run(() => analyzeImage(bytes));
      if (token != _token) return;    // 分析期間又換了圖片
      candidates = result;
    } on ImageDecodeException catch (e) {
      if (token != _token) return;
      candidates = null; error = e.message;
    } finally {
      if (token == _token) { busy = false; notifyListeners(); }
    }
  }

  void setCount(int value) { count = value.clamp(0, kMaxColors); notifyListeners(); }
  void setMood(ColorMood value) { mood = value; notifyListeners(); }
}
```

狀態管理工具可以依專案慣例換成 Riverpod、Bloc 等，但要保留兩個行為：**快取候選色**，以及用 **token 丟棄過期的結果**。

### 5.7 無障礙
- Stepper 的按鈕加上 `Semantics(label: '減少顏色數量' / '增加顏色數量')`，數字變動時要讓螢幕閱讀器念出來（`liveRegion: true`）。
- 圖片面板標示為「上傳的圖片」，色塊列表標示為「擷取出的顏色」，每個色塊提示「點擊複製色碼」。
- 狀態列設定 `liveRegion: true`。

---

## 6. 測試與驗收

### 6.1 準備：產生 Python 參考資料
先把 `color_cluster.py` 拆成兩個函式，`extract_colors` 的行為維持不變：
- `compute_candidates(image) -> (candidate_colors, candidate_scores)`：步驟 ①～⑥
- `select_palette(candidate_colors, candidate_scores, n_colors, mood)`：步驟 ⑦～⑧

再用以下腳本（`tool/export_fixtures.py`）對 `data/sample_*.jpg` 產生 JSON 參考資料：

```python
import json, pathlib, cv2, numpy as np
from color_cluster import compute_candidates, select_palette, MOODS, MAX_COLORS

out = {}
for path in sorted(pathlib.Path('data').glob('sample_*.*')):
    colors, scores = compute_candidates(str(path))
    out[path.name] = {
        'candidates': [{'rgb': c.tolist(), 'score': float(s)} for c, s in zip(colors, scores)],
        'palettes': {
            mood: {str(n): select_palette(colors, scores, n, mood) for n in range(MAX_COLORS + 1)}
            for mood in MOODS
        },
    }
pathlib.Path('test/fixtures/reference.json').write_text(json.dumps(out, indent=1, default=list))
```

另外準備一張小的合成圖片（例如 7×5，含純黑、純白、灰、高飽和色），匯出 cv2 的 `gray`、HSV 的 `S`／`V`、`Laplacian` 結果，供 §6.2 的逐像素測試使用。

### 6.2 測試項目

| 層級 | 測試 | 通過標準 |
|---|---|---|
| 單元 | `gray`、`hsvS`、`hsvV` 對全部 256³ 種顏色中隨機抽 10 萬個，和 cv2 比對 | **完全相同** |
| 單元 | Laplacian（含四個邊角）和合成圖片的參考值比對 | 誤差 < 1e-9 |
| 單元 | `resizeArea` 縮小時和 cv2 INTER_AREA 比對 | 每個通道差 ≤ 1 |
| 單元 | `selectPalette` 輸入 Python 的候選色，比對全部 mood × n = 0..8 | 色碼與順序**完全相同**，ratio 誤差 < 1e-9 |
| 單元 | K-Means：明顯分開的合成資料（例如 3 團各 1000 點） | 找到正確的 3 個中心；同樣輸入跑兩次結果相同 |
| 單元 | 邊界情況（§3.10 每一列） | 行為符合表格 |
| 整合 | `analyzeImage` 跑每張範例圖，和 Python 候選色做一對一配對（貪婪法，取 RGB 距離最近的配對） | 每組配對的 RGB 歐氏距離 ≤ 30，且平均 ≤ 12 |
| 整合 | 直拍的手機照片（有 EXIF orientation）、透明 PNG、16-bit PNG、灰階 JPEG | 不會發生錯誤，結果和 Python 相符（同上標準） |
| Widget | Stepper 上下限、Mood 切換、空狀態文字、點擊複製後顯示「已複製」 | 符合 §5 |
| 效能 | §4 的效能目標 | ≤ 1.5 秒 |

整合測試的容許誤差是因為 K-Means 的亂數序列無法和 numpy 一致（§3.7.2）。超出標準的圖片要人工比對色塊，確認視覺上是否等價，再決定要不要放寬標準。

---

## 7. 建議的檔案結構

```
lib/
  palette/
    constants.dart          常數（§2）
    models.dart             ColorMood、ColorCandidate、PaletteColor、ImageDecodeException
    color_space.dart        gray、hsvS、hsvV（§3.5）
    image_preprocess.dart   decodeRgb、resizeArea、邊緣與顯著性（§3.3 ~ 3.6）
    weighted_kmeans.dart    K-Means（§3.7）
    color_extractor.dart    analyzeImage、selectPalette、extractColors（§3.8 ~ 3.9）
  ui/
    palette_theme.dart      色彩 Token（§5.2）
    palette_controller.dart （§5.6）
    palette_page.dart       響應式版面（§5.1）
    widgets/
      header_bar.dart
      count_stepper.dart
      image_panel.dart      含選圖與拖曳
      swatch_list.dart
      swatch_tile.dart
test/
  fixtures/reference.json
  palette/...               單元與整合測試
  ui/...                    Widget 測試
tool/
  export_fixtures.py
```

`lib/palette/` 不能 import `package:flutter`，維持純 Dart，才能在 Isolate 中執行，也能用 `dart test` 快速測試。

---

## 8. 工作拆分

1. [ ] Python 端拆函式並匯出參考資料（§6.1）
2. [ ] `color_space.dart` + 測試
3. [ ] `image_preprocess.dart`（解碼、縮放、邊緣、顯著性）+ 測試
4. [ ] `weighted_kmeans.dart`（含合併重複顏色）+ 測試
5. [ ] `color_extractor.dart` + 和 Python 的比對測試
6. [ ] 效能量測，必要時優化（§4）
7. [ ] 主題、Controller、版面與各元件
8. [ ] 各平台選圖（行動裝置相簿與相機、桌面版檔案對話框與拖曳）
9. [ ] Widget 測試與無障礙檢查
10. [ ] 真機驗收（Android、iOS，以及要支援的桌面平台）
