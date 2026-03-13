"""
PMOS 滑动验证码处理模块
功能：获取验证码 + 滑动滑块完整流程

核心原理：二维边缘轮廓形状匹配 - 专杀同水平线上1真多假缺口

使用示例:
    from z import CaptchaHandler

    handler = CaptchaHandler(debug=True)
    success = handler.solve_captcha(page, slider_element)
"""

import numpy as np
import os
import time
import random
import base64
from typing import Optional, Tuple, Dict, Any, List

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False
    print("提示: opencv-python 未安装")


# ==================== 常量定义 ====================

class CaptchaConstants:
    """验证码相关常量"""

    # 默认滑块宽度（像素）
    DEFAULT_SLIDER_WIDTH = 60

    # 滑块宽度范围
    SLIDER_WIDTH_MIN = 40
    SLIDER_WIDTH_MAX = 100

    # 搜索范围（缺口位置）
    GAP_SEARCH_START_X = 50
    GAP_SEARCH_END_RATIO = 0.7
    GAP_SEARCH_MAX_X = 300

    # 基础补偿比例（滑块宽度的百分比）
    BASE_OFFSET_RATIO = 0.12

    # 自适应补偿范围（像素）
    ADAPTIVE_OFFSET_MIN = -15
    ADAPTIVE_OFFSET_MAX = 10

    # 最大重试次数
    MAX_RETRY = 6

    # 验证码检查等待时间（秒）
    CAPTCHA_CHECK_WAIT = 1.5
    CAPTCHA_REFRESH_WAIT = 0.5


class DragConstants:
    """拖动轨迹相关常量"""

    # 延迟时间（毫秒）
    DELAY_START_MIN = 8
    DELAY_START_MAX = 20
    DELAY_MIDDLE_MIN = 6
    DELAY_MIDDLE_MAX = 15
    DELAY_END_MIN = 20
    DELAY_END_MAX = 50

    # 随机偏移（像素）
    RANDOM_OFFSET_MIN = -2
    RANDOM_OFFSET_MAX = 2
    Y_JITTER_MIN = -1
    Y_JITTER_MAX = 1


# ==================== 验证码识别核心类 ====================

class CaptchaSolver:
    """验证码求解器 - 二维边缘轮廓匹配"""

    # 自适应补偿相关（类变量）
    _global_offset_history = []
    _success_count = 0
    _failure_count = 0

    def __init__(self, debug=True, max_retry=None, enable_adaptive=True):
        self.has_opencv = HAS_OPENCV
        self.debug = debug
        self.debug_dir = "debug_captcha"
        self.max_retry = max_retry or CaptchaConstants.MAX_RETRY
        self.enable_adaptive = enable_adaptive

        # 补偿参数
        self.base_offset_ratio = CaptchaConstants.BASE_OFFSET_RATIO
        self.adaptive_offset = 0
        self.offset_range = (
            CaptchaConstants.ADAPTIVE_OFFSET_MIN,
            CaptchaConstants.ADAPTIVE_OFFSET_MAX
        )

        if debug:
            os.makedirs(self.debug_dir, exist_ok=True)

    def record_result(self, offset, success):
        """记录验证结果用于自适应学习"""
        if not self.enable_adaptive:
            return

        CaptchaSolver._global_offset_history.append({'offset': offset, 'success': success})

        if success:
            CaptchaSolver._success_count += 1
            if len(CaptchaSolver._global_offset_history) > 10:
                CaptchaSolver._global_offset_history = [
                    r for r in CaptchaSolver._global_offset_history
                    if r['success'] or len(CaptchaSolver._global_offset_history) < 20
                ]
        else:
            CaptchaSolver._failure_count += 1

        self._update_adaptive_offset()

    def _update_adaptive_offset(self):
        """根据历史记录更新自适应补偿值"""
        success_records = [r for r in CaptchaSolver._global_offset_history if r['success']]

        if len(success_records) >= 3:
            weights = [0.5 ** i for i in range(len(success_records)-1, -1, -1)]
            weighted_sum = sum(r['offset'] * w for r, w in zip(success_records, weights))
            total_weight = sum(weights)

            new_offset = weighted_sum / total_weight
            new_offset = max(self.offset_range[0], min(self.offset_range[1], new_offset))

            if abs(new_offset - self.adaptive_offset) > 1:
                print(f"[自适应] 补偿值调整: {self.adaptive_offset:.1f} -> {new_offset:.1f}px")

            self.adaptive_offset = new_offset

    def get_adaptive_suggestions(self, base_distance):
        """获取自适应补偿建议"""
        suggestions = []

        if abs(self.adaptive_offset) > 0.5:
            suggestions.append(int(self.adaptive_offset))

        success_records = [r for r in CaptchaSolver._global_offset_history if r['success']]
        if success_records:
            recent_offsets = [int(r['offset']) for r in success_records[-5:]]
            for offset in recent_offsets:
                if offset not in suggestions:
                    suggestions.append(offset)

        common_offsets = [0, -2, 2, -4, 4, -6, 6, -8, 8, -10, 10]
        for offset in common_offsets:
            if offset not in suggestions:
                suggestions.append(offset)

        return suggestions[:15]

    @classmethod
    def reset_adaptive(cls):
        """重置自适应学习数据"""
        cls._global_offset_history = []
        cls._success_count = 0
        cls._failure_count = 0

    # ==================== 核心识别方法 ====================

    def get_slide_distance(self, bg_image, slider_image=None, slider_info=None):
        """
        专杀同一水平线上 1真多假 验证码
        核心原理：二维边缘形状交叉互相关匹配 + 阴影补偿

        Args:
            bg_image: 背景图片（bytes或numpy数组）
            slider_image: 滑块图片（bytes或numpy数组，竖条RGBA格式）
            slider_info: 滑块信息字典

        Returns:
            (gap_x, gap_y, confidence) 缺口位置和置信度
        """
        if not self.has_opencv:
            return (180, None, 0.5)

        bg_img = self._decode_image(bg_image)
        if bg_img is None:
            return (180, None, 0.5)

        h, w = bg_img.shape[:2]

        if self.debug:
            cv2.imwrite(f"{self.debug_dir}/01_original_bg.png", bg_img)

        # 提取滑块真实形状和 Y 坐标
        if slider_image is None:
            print("[二维匹配] 无滑块图片，回退到边缘检测")
            return self._fallback_edge_detection(bg_img, slider_info)

        y_start, y_end, x_start, slider_rgba = self._extract_slider_info_rgba(slider_image)

        if slider_rgba is None:
            print("[二维匹配] 提取滑块形状失败，回退到边缘检测")
            return self._fallback_edge_detection(bg_img, slider_info)

        # 截取背景图的 Y 轴水平带
        strip_y_start = max(0, y_start - 5)
        strip_y_end = min(h, y_end + 5)
        bg_strip = bg_img[strip_y_start:strip_y_end, :]

        # 处理背景边缘
        bg_gray = cv2.cvtColor(bg_strip, cv2.COLOR_BGR2GRAY)
        bg_blur = cv2.GaussianBlur(bg_gray, (3, 3), 0)
        bg_edges = cv2.Canny(bg_blur, 50, 150)

        # 处理滑块边缘
        slider_gray = cv2.cvtColor(slider_rgba, cv2.COLOR_BGRA2GRAY)
        slider_blur = cv2.GaussianBlur(slider_gray, (3, 3), 0)
        slider_edges = cv2.Canny(slider_blur, 50, 150)

        # 消除滑块外围方形黑边的干扰
        alpha_channel = slider_rgba[:, :, 3]
        _, mask = cv2.threshold(alpha_channel, 10, 255, cv2.THRESH_BINARY)

        kernel = np.ones((2, 2), np.uint8)
        mask_eroded = cv2.erode(mask, kernel, iterations=1)
        slider_edges = cv2.bitwise_and(slider_edges, mask_eroded)

        # 二维交叉互相关匹配
        result = cv2.matchTemplate(bg_edges, slider_edges, cv2.TM_CCOEFF_NORMED)

        # 排除左侧初始坑位
        ignore_width = slider_rgba.shape[1] + 20
        result[:, :ignore_width] = -1.0

        # 找出匹配度最高的位置
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        best_x = max_loc[0]
        best_y = (y_start + y_end) // 2

        print(f"[二维轮廓匹配] 原始最佳X: {best_x}, 匹配得分: {max_val:.3f}")

        # 距离补偿逻辑
        shadow_compensation = 8
        initial_padding = x_start if x_start else 0
        compensated_x = best_x + shadow_compensation
        compensated_x = max(ignore_width, min(compensated_x, w - slider_rgba.shape[1]))

        print(f"[二维轮廓匹配] 补偿后最终滑动距离: {compensated_x} (阴影补偿+{shadow_compensation}px)")

        # 保存调试图
        if self.debug:
            cv2.imwrite(f"{self.debug_dir}/02_bg_edges_strip.png", bg_edges)
            cv2.imwrite(f"{self.debug_dir}/03_slider_edges_clean.png", slider_edges)

            debug_img = bg_img.copy()
            cv2.rectangle(debug_img, (0, strip_y_start), (w, strip_y_end), (255, 0, 0), 1)
            cv2.rectangle(debug_img, (best_x, strip_y_start), (best_x + slider_rgba.shape[1], strip_y_end), (0, 0, 255), 1)
            cv2.rectangle(debug_img, (compensated_x, strip_y_start), (compensated_x + slider_rgba.shape[1], strip_y_end), (0, 255, 0), 2)
            cv2.circle(debug_img, (best_x, best_y), 5, (0, 0, 255), -1)
            cv2.imwrite(f"{self.debug_dir}/04_final_match.png", debug_img)

        return (compensated_x, best_y, max_val)

    def _extract_slider_info_rgba(self, slider_img_data):
        """解析竖向滑块图，提取精确的 Y 坐标和滑块RGBA图"""
        if not self.has_opencv:
            return None, None, None, None

        if isinstance(slider_img_data, bytes):
            arr = np.frombuffer(slider_img_data, np.uint8)
            slider_img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        elif isinstance(slider_img_data, str):
            slider_img = cv2.imread(slider_img_data, cv2.IMREAD_UNCHANGED)
        else:
            slider_img = slider_img_data

        if slider_img is None or len(slider_img.shape) < 3 or slider_img.shape[2] != 4:
            return None, None, None, None

        alpha_channel = slider_img[:, :, 3]

        y_coords, x_coords = np.where(alpha_channel > 0)
        if len(y_coords) == 0:
            return None, None, None, None

        y_start, y_end = np.min(y_coords), np.max(y_coords)
        x_start, x_end = np.min(x_coords), np.max(x_coords)

        cropped_slider_rgba = slider_img[y_start:y_end+1, x_start:x_end+1]

        print(f"[滑块提取] 原始尺寸: {slider_img.shape}, 滑块范围: Y=[{y_start},{y_end}], X偏移={x_start}")

        if self.debug:
            display_img = cropped_slider_rgba.copy()
            white_bg = np.ones((display_img.shape[0], display_img.shape[1], 3), dtype=np.uint8) * 255
            mask = display_img[:, :, 3] > 0
            white_bg[mask] = display_img[mask, :3]
            cv2.imwrite(f"{self.debug_dir}/01_slider_cropped.png", white_bg)

        return y_start, y_end, x_start, cropped_slider_rgba

    def _fallback_edge_detection(self, bg_img, slider_info):
        """备用方法：当没有滑块图片时，使用边缘检测"""
        h, w = bg_img.shape[:2]

        y_start, y_end = 0, h
        slider_width = 60

        if slider_info:
            if 'y' in slider_info and slider_info['y'] > 0:
                slider_y = slider_info['y']
                y_start = max(0, int(slider_y) - 10)
                y_end = min(h, int(slider_y) + slider_width + 10)

        if y_start == 0 and y_end == h:
            y_start, y_end = self._detect_slider_y_from_bg(bg_img, w, h)

        # 边缘检测
        gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)

        if y_end > y_start and (y_end - y_start) < h * 0.8:
            mask = np.zeros_like(edges)
            mask_y_start = max(0, y_start - 5)
            mask_y_end = min(h, y_end + 5)
            mask[mask_y_start:mask_y_end, :] = 255
            edges = cv2.bitwise_and(edges, mask)

        gap_x = self._detect_gap_x_by_edge(edges, h, w, slider_width)

        if gap_x:
            gap_y = (y_start + y_end) // 2
            return (gap_x, gap_y, 0.7)

        return (min(w - 50, 200), h // 2, 0.3)

    def _detect_slider_y_from_bg(self, img, w, h):
        """从背景图检测滑块Y轴范围"""
        left_roi = img[:, :min(80, w // 4)]
        gray = cv2.cvtColor(left_roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)

        row_edges = np.sum(edges, axis=1)
        smoothed = np.convolve(row_edges, np.ones(5)/5, mode='same')

        if np.max(smoothed) > 0:
            center_y = np.argmax(smoothed)
            half_h = 30
            y_start = max(0, center_y - half_h - 10)
            y_end = min(h, center_y + half_h + 10)
            return y_start, y_end

        return 0, h

    def _detect_gap_x_by_edge(self, edges, h, w, slider_width):
        """通过边缘的水平投影检测缺口的X位置"""
        col_edges = np.sum(edges, axis=0)

        col_edges_smooth1 = np.convolve(col_edges, np.ones(3) / 3, mode='same')
        col_edges_smooth2 = np.convolve(col_edges, np.ones(7) / 7, mode='same')
        col_edges_smooth = (col_edges_smooth1 * 0.6 + col_edges_smooth2 * 0.4)

        start_x = 50
        end_x = min(w - 50, max(300, int(w * 0.7)))

        region_edges = col_edges_smooth[start_x:end_x]
        base_mean = np.mean(region_edges)
        base_std = np.std(region_edges)

        candidates = []

        for x in range(start_x, end_x):
            val = col_edges_smooth[x]

            threshold = base_mean + base_std * 0.8
            if val < threshold:
                continue

            is_peak = True
            peak_range = 7
            for dx in range(-peak_range, peak_range + 1):
                if dx == 0:
                    continue
                nx = x + dx
                if 0 <= nx < w:
                    if col_edges_smooth[nx] > val:
                        is_peak = False
                        break

            if is_peak:
                score = val - base_mean
                neighborhood_mean = np.mean(col_edges_smooth[max(0, x-5):min(w, x+6)])
                sharpness = val / (neighborhood_mean + 1)
                candidates.append((x, score * sharpness))

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[1], reverse=True)
        best_x, best_score = candidates[0]

        base_offset = int(slider_width * self.base_offset_ratio)
        total_offset = base_offset + self.adaptive_offset

        compensated_x = best_x - total_offset
        compensated_x = max(start_x, min(compensated_x, end_x))

        print(f"[边缘检测X] 峰值={best_x}, 补偿后={compensated_x}")

        return compensated_x

    def _decode_image(self, image):
        """解码图片"""
        if isinstance(image, bytes):
            arr = np.frombuffer(image, np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        elif isinstance(image, np.ndarray):
            return image
        return None


# ==================== 验证码处理主类 ====================

class CaptchaHandler:
    """验证码处理器 - 完整的获取验证码和滑动流程"""

    def __init__(self, debug=True, max_retry=None):
        """
        初始化验证码处理器

        Args:
            debug: 是否开启调试模式（保存调试图）
            max_retry: 最大重试次数
        """
        self.debug = debug
        self.captcha_solver = CaptchaSolver(debug=debug, max_retry=max_retry)

    def find_slider_element(self, page):
        """
        查找滑块元素

        Args:
            page: Playwright页面对象

        Returns:
            滑块元素或None
        """
        captcha_configs = [
            {"slider": ".verify-move-block", "track": ".verify-left-bar", "check": ".verify-bar-area"},
            {"slider": "#nc_1__scale_text", "track": ".nc_1__scale_text", "check": ".yidun_intelligence"},
            {"slider": "[class*='slider-btn']", "track": "[class*='slider-track']", "check": "[class*='captcha']"},
        ]

        for config in captcha_configs:
            try:
                if page.query_selector(config["check"]):
                    slider = page.wait_for_selector(config["slider"], timeout=1000)
                    if slider:
                        return slider
            except:
                continue

        # 模糊匹配
        fuzzy_selectors = ["[class*='slider-btn']", "[class*='slide-btn']", ".yidun_slider__icon"]
        for selector in fuzzy_selectors:
            try:
                elements = page.query_selector_all(selector)
                for elem in elements:
                    if elem.is_visible():
                        return elem
            except:
                continue

        return None

    def get_captcha_images(self, page, container):
        """
        获取验证码图片

        Args:
            page: Playwright页面对象
            container: 验证码容器元素

        Returns:
            (bg_image, block_image, img_offset) 背景图、滑块图和偏移信息
        """
        bg_image = None
        block_image = None
        img_offset = None

        try:
            result = page.evaluate("""
                () => {
                    const allImages = [];
                    const selectors = [
                        '.verify-img-panel img',
                        '.verify-img-out img',
                        '.verify-area img',
                        '.verify-img-panel canvas',
                        '.verify-img-out canvas',
                        '.verify-area canvas'
                    ];

                    for (const sel of selectors) {
                        const elems = document.querySelectorAll(sel);
                        elems.forEach((el, idx) => {
                            if (el.offsetParent !== null) {
                                const rect = el.getBoundingClientRect();
                                let src = null;

                                if (el.tagName === 'CANVAS') {
                                    try {
                                        src = el.toDataURL('image/png');
                                    } catch(e) {}
                                } else if (el.tagName === 'IMG') {
                                    src = el.src;
                                }

                                if (src) {
                                    const container = document.querySelector('.verify-img-panel, .verify-img-out, .verify-area');
                                    let offsetX = 0, offsetY = 0;
                                    if (container) {
                                        const containerRect = container.getBoundingClientRect();
                                        offsetX = rect.left - containerRect.left;
                                        offsetY = rect.top - containerRect.top;
                                    }

                                    allImages.push({
                                        src: src,
                                        width: Math.round(rect.width),
                                        height: Math.round(rect.height),
                                        area: Math.round(rect.width * rect.height),
                                        offsetX: Math.round(offsetX),
                                        offsetY: Math.round(offsetY)
                                    });
                                }
                            }
                        });
                    }

                    allImages.sort((a, b) => b.area - a.area);
                    return { count: allImages.length, images: allImages };
                }
            """)

            if result:
                images = result.get('images', [])
                if images:
                    img_info = images[0]
                    src = img_info.get('src')
                    if src:
                        bg_image = self._fetch_image_data(page, src)
                        img_offset = {
                            'x': img_info.get('offsetX', 0),
                            'y': img_info.get('offsetY', 0),
                            'width': img_info.get('width', 0),
                            'height': img_info.get('height', 0)
                        }

        except Exception as e:
            print(f"[验证码] 获取背景图出错: {e}")

        # 获取滑块拼图
        try:
            slider_bg = page.evaluate("""
                () => {
                    const slider = document.querySelector('.verify-move-block');
                    if (!slider) return null;

                    const style = window.getComputedStyle(slider);
                    const bgImage = style.backgroundImage;

                    if (bgImage && bgImage !== 'none') {
                        const match = bgImage.match(/url\\(['"]?([^'"]+)['"]?\\)/);
                        if (match) {
                            return { src: match[1], method: 'background' };
                        }
                    }

                    const img = slider.querySelector('img');
                    if (img && img.src) {
                        return { src: img.src, method: 'child_img' };
                    }

                    return null;
                }
            """)

            if slider_bg:
                src = slider_bg.get('src')
                if src:
                    block_image = self._fetch_image_data(page, src)

        except Exception as e:
            print(f"[验证码] 获取拼图块出错: {e}")

        # 兜底
        if not bg_image:
            try:
                bg_image = container.screenshot()
            except:
                pass

        return bg_image, block_image, img_offset

    def _fetch_image_data(self, page, src):
        """获取图片数据"""
        if not src:
            return None

        if src.startswith('data:image'):
            try:
                return base64.b64decode(src.split(',', 1)[1])
            except:
                pass

        try:
            result = page.evaluate(f"""
                async () => {{
                    const url = '{src}';
                    try {{
                        const response = await fetch(url);
                        const blob = await response.blob();
                        return new Promise((resolve) => {{
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        }});
                    }} catch(e) {{
                        return null;
                    }}
                }}
            """)
            if result and result.startswith('data:image'):
                return base64.b64decode(result.split(',', 1)[1])
        except:
            pass

        return None

    def get_captcha_positions(self, page, slider_element):
        """
        获取验证码各元素位置

        Args:
            page: Playwright页面对象
            slider_element: 滑块元素

        Returns:
            (slider_box, track_box, container_box, bg_image, block_image, img_offset)
        """
        slider_box = slider_element.bounding_box()
        track = page.query_selector(".verify-bar-area, .verify-left-bar")
        track_box = track.bounding_box() if track else None
        container = page.query_selector(".verify-img-panel, .verify-img-out, .verify-area")
        container_box = container.bounding_box() if container else None

        if not (slider_box and track_box and container_box):
            return None

        bg_image, block_image, img_offset = self.get_captcha_images(page, container)

        print(f"[定位] 滑块位置: x={slider_box['x']:.1f}, width={slider_box['width']:.1f}")
        print(f"[定位] 容器位置: x={container_box['x']:.1f}, width={container_box['width']:.1f}")
        if img_offset:
            print(f"[定位] 图片相对容器偏移: x={img_offset['x']:.1f}, y={img_offset['y']:.1f}")

        return slider_box, track_box, container_box, bg_image, block_image, img_offset

    def simulate_drag(self, page, slider_element, distance):
        """
        模拟拖动滑块

        Args:
            page: Playwright页面对象
            slider_element: 滑块元素
            distance: 滑动距离

        Returns:
            是否成功拖动
        """
        if not distance or distance < 50:
            distance = 200

        slider_box = slider_element.bounding_box()
        if not slider_box:
            return False

        start_x = slider_box['x'] + slider_box['width'] / 2
        start_y = slider_box['y'] + slider_box['height'] / 2

        print(f"[拖动] 起点=({start_x:.1f}, {start_y:.1f}), 距离={distance:.1f}px")

        tracks = self._generate_drag_tracks(distance)

        try:
            offset_x = random.randint(DragConstants.RANDOM_OFFSET_MIN, DragConstants.RANDOM_OFFSET_MAX)
            offset_y = random.randint(DragConstants.RANDOM_OFFSET_MIN, DragConstants.RANDOM_OFFSET_MAX)
            page.mouse.move(start_x + offset_x, start_y + offset_y)
            time.sleep(random.randint(100, 300) / 1000)

            page.mouse.down()
            time.sleep(random.randint(80, 150) / 1000)

            for i, track in enumerate(tracks):
                y_jitter = random.randint(DragConstants.Y_JITTER_MIN, DragConstants.Y_JITTER_MAX)
                page.mouse.move(start_x + track['x'], start_y + y_jitter)

                progress = i / len(tracks)
                if progress < 0.3:
                    time.sleep(track['delay'] * 0.8)
                elif progress < 0.7:
                    time.sleep(track['delay'])
                else:
                    time.sleep(track['delay'] * 1.2)

            time.sleep(random.randint(50, 150) / 1000)
            page.mouse.up()
            print(f"[拖动] 完成，共{len(tracks)}步")
            return True

        except Exception as e:
            print(f"[拖动] 出错: {e}")
            return False

    def _generate_drag_tracks(self, distance):
        """
        生成拖动轨迹 - 模拟人类真实行为

        Args:
            distance: 滑动距离

        Returns:
            轨迹点列表
        """
        tracks = []

        # 随机选择人类行为模式
        behavior = random.choice(['normal', 'normal', 'normal', 'cautious', 'fast', 'jitter'])

        # 根据行为模式设置参数
        if behavior == 'normal':
            num_points = random.randint(20, 30)
            base_speed = 1.0
            jitter_amount = 0.5
        elif behavior == 'cautious':
            num_points = random.randint(30, 45)
            base_speed = 1.3
            jitter_amount = 0.3
        elif behavior == 'fast':
            num_points = random.randint(15, 22)
            base_speed = 0.7
            jitter_amount = 0.8
        else:  # jitter
            num_points = random.randint(25, 40)
            base_speed = 1.0
            jitter_amount = 1.5

        for i in range(num_points):
            progress = i / (num_points - 1)

            # 使用更自然的缓动曲线 - easeInOutCubic
            if progress < 0.5:
                eased = 4 * progress * progress * progress
            else:
                eased = 1 - pow(-2 * progress + 2, 3) / 2

            # 添加人类行为特征
            if behavior == 'cautious':
                if progress < 0.2:
                    eased *= 0.7
                elif progress > 0.7:
                    eased = 0.7 + (eased - 0.7) * 0.5
            elif behavior == 'fast':
                if progress < 0.3:
                    eased *= 1.2
            elif behavior == 'jitter':
                jitter = random.uniform(-jitter_amount / 100, jitter_amount / 100)
                eased = max(0, min(1, eased + jitter))

            # 计算位置
            base_x = distance * eased

            if random.random() < 0.25:
                base_x += random.uniform(-jitter_amount, jitter_amount)

            x = int(base_x)

            # 计算延迟
            if progress < 0.1:
                delay = random.randint(DragConstants.DELAY_START_MIN, DragConstants.DELAY_START_MAX) * base_speed
            elif progress < 0.25:
                delay = random.randint(8, 15) * base_speed
            elif progress < 0.7:
                delay = random.randint(DragConstants.DELAY_MIDDLE_MIN, DragConstants.DELAY_MIDDLE_MAX) * base_speed
            elif progress < 0.85:
                delay = random.randint(12, 25) * base_speed
            else:
                delay = random.randint(DragConstants.DELAY_END_MIN, DragConstants.DELAY_END_MAX) * base_speed

            tracks.append({'x': x, 'delay': delay / 1000})

        # 终点精确定位
        tracks.append({'x': distance - random.randint(1, 3), 'delay': random.randint(40, 70) / 1000})
        tracks.append({'x': distance, 'delay': random.randint(60, 120) / 1000})

        # 过冲回调
        if random.random() < 0.4:
            overshoot = random.randint(1, 3)
            tracks.append({'x': distance + overshoot, 'delay': random.randint(30, 60) / 1000})
            tracks.append({'x': distance, 'delay': random.randint(50, 100) / 1000})

        return tracks

    def check_captcha_passed(self, page, slider_element):
        """
        检查验证码是否通过

        Args:
            page: Playwright页面对象
            slider_element: 滑块元素

        Returns:
            是否通过
        """
        try:
            # 检查滑块是否消失
            if not slider_element.is_visible():
                print("[验证] 滑块已消失，可能通过")
                return True

            # 检查成功标志
            result = page.evaluate("""
                () => {
                    const iconSelectors = [
                        '.verify-icon-success', '.icon-success', '.success-icon',
                        '[class*="icon-success"]', '[class*="success-icon"]',
                        '[class*="success"]', '.success', '.passed', '.pass'
                    ];

                    for (const sel of iconSelectors) {
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {
                            return {type: 'icon', selector: sel};
                        }
                    }

                    const textSelectors = ['.verify-success', '.success-text', '.pass-text'];
                    for (const sel of textSelectors) {
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {
                            const text = (elem.textContent || '').trim();
                            if (text.includes('通过') || text.includes('成功')) {
                                return {type: 'text', selector: sel};
                            }
                        }
                    }

                    const container = document.querySelector('.verify-bar-area, .verify-left-bar, [class*="slider"]');
                    if (container) {
                        const className = container.className || '';
                        if (className.includes('success') || className.includes('pass')) {
                            return {type: 'container'};
                        }
                    }

                    const mask = document.querySelector('.verify-mask, .mask');
                    if (!mask || (mask.style && (mask.style.display === 'none' || mask.style.opacity === '0'))) {
                        return {type: 'mask_gone'};
                    }

                    return null;
                }
            """)

            if result:
                print(f"[验证] 检测到通过状态: {result}")
                return True

            return False

        except Exception as e:
            print(f"[验证] 检测出错: {e}")
            return False

    def check_slider_returned(self, page, slider_element):
        """检查滑块是否回到起始位置"""
        try:
            slider_box = slider_element.bounding_box()
            if not slider_box:
                return False

            track = page.query_selector(".verify-bar-area, .verify-left-bar")
            if track:
                track_box = track.bounding_box()
                if track_box and abs(slider_box['x'] - track_box['x']) < 10:
                    return True

            return False
        except:
            return False

    def solve_captcha(self, page, slider_element=None, max_attempts=None):
        """
        完整的验证码求解流程

        Args:
            page: Playwright页面对象
            slider_element: 滑块元素（如果为None则自动查找）
            max_attempts: 最大尝试次数

        Returns:
            是否验证成功
        """
        if slider_element is None:
            slider_element = self.find_slider_element(page)
            if not slider_element:
                print("[验证码] 未找到滑块元素")
                return False

        max_attempts = max_attempts or self.captcha_solver.max_retry

        print("\n" + "="*50)
        print("开始验证码求解")
        print("="*50)

        for attempt in range(max_attempts):
            print(f"\n=== 第 {attempt + 1}/{max_attempts} 次尝试 ===")

            # 获取位置信息
            captcha_info = self.get_captcha_positions(page, slider_element)
            if not captcha_info:
                print("[验证码] 无法获取位置信息")
                break

            slider_box, track_box, container_box, bg_image, block_image, img_offset = captcha_info

            if not bg_image:
                print("[验证码] 无法获取验证码图片")
                break

            # 滑块信息
            slider_info = {
                'x': slider_box['x'],
                'y': slider_box['y'],
                'width': slider_box['width'],
                'height': slider_box['height']
            }

            # 识别缺口位置
            result = self.captcha_solver.get_slide_distance(bg_image, block_image, slider_info)

            # 解析结果
            if isinstance(result, tuple) and len(result) >= 1:
                gap_x_in_image = result[0]
                gap_y_in_image = result[1] if len(result) >= 2 else None
                confidence = result[2] if len(result) >= 3 else 0.5
            else:
                gap_x_in_image = result
                gap_y_in_image = None
                confidence = 0.5

            if not gap_x_in_image:
                print("[验证码] 未找到匹配的缺口")
                break

            # 计算距离
            slider_left_x = slider_box['x']
            img_offset_x = img_offset['x'] if img_offset else 0
            gap_left_screen_x = container_box['x'] + img_offset_x + gap_x_in_image

            base_distance = gap_left_screen_x - slider_left_x

            print(f"[验证码] 计算距离: {base_distance:.1f}px")

            # 获取自适应补偿建议
            adjustments = self.captcha_solver.get_adaptive_suggestions(base_distance)
            print(f"[自适应] 调整序列: {adjustments[:10]}")

            tried_adjustments = set()
            max_tries_per_attempt = 8

            for adj in adjustments:
                if adj in tried_adjustments:
                    continue
                if len(tried_adjustments) >= max_tries_per_attempt:
                    break

                tried_adjustments.add(adj)

                final_distance = base_distance + adj
                final_distance = max(30, min(400, final_distance))

                print(f"[验证码] 滑动: {final_distance:.1f}px (调整: {adj:+d})")

                if not self.simulate_drag(page, slider_element, final_distance):
                    break

                # 等待验证结果
                time.sleep(CaptchaConstants.CAPTCHA_CHECK_WAIT)

                # 检查是否通过
                passed = self.check_captcha_passed(page, slider_element)
                if passed:
                    print("\n" + "="*50)
                    print(f"[验证码] 通过! (调整: {adj:+d}px)")
                    print("="*50)
                    self.captcha_solver.record_result(adj, True)
                    return True

                # 检查是否失败了
                if self.check_slider_returned(page, slider_element):
                    print(f"[验证码] 滑块回到原位，调整值 {adj:+d} 无效，尝试下一个")
                    self.captcha_solver.record_result(adj, False)
                    time.sleep(0.5)
                    continue

                print("[验证码] 未通过，继续尝试...")
                time.sleep(0.3)

            # 等待验证码刷新
            print("[验证码] 等待验证码刷新...")
            time.sleep(2)

        print("\n[验证码] 求解结束")
        return False

    def wait_and_solve(self, page, max_wait=300, check_interval=2):
        """
        等待验证码出现并自动求解

        Args:
            page: Playwright页面对象
            max_wait: 最大等待时间（秒）
            check_interval: 检查间隔（秒）

        Returns:
            是否验证成功
        """
        waited = 0

        print("[轮询] 等待滑块验证码出现...")

        while waited < max_wait:
            slider_element = self.find_slider_element(page)
            if slider_element:
                print(f"\n[检测] 发现滑块验证码！(等待 {waited} 秒后)")
                return self.solve_captcha(page, slider_element)

            time.sleep(check_interval)
            waited += check_interval
            if waited % 10 == 0:
                print(f"[轮询] 等待中... ({waited}/{max_wait}秒)")

        print("[超时] 未检测到滑块验证码")
        return False


# ==================== 便捷函数 ====================

def solve_captcha(page, debug=True):
    """
    便捷函数：求解验证码

    Args:
        page: Playwright页面对象
        debug: 是否开启调试模式

    Returns:
        是否验证成功
    """
    handler = CaptchaHandler(debug=debug)
    return handler.solve_captcha(page)


def wait_and_solve_captcha(page, max_wait=300, debug=True):
    """
    便捷函数：等待并求解验证码

    Args:
        page: Playwright页面对象
        max_wait: 最大等待时间（秒）
        debug: 是否开启调试模式

    Returns:
        是否验证成功
    """
    handler = CaptchaHandler(debug=debug)
    return handler.wait_and_solve(page, max_wait=max_wait)


# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("PMOS 滑动验证码处理模块")
    print("\n使用方法:")
    print("1. 导入 CaptchaHandler")
    print("2. 创建实例: handler = CaptchaHandler(debug=True)")
    print("3. 调用方法: handler.solve_captcha(page)")
    print("\n或者使用便捷函数:")
    print("  from z import solve_captcha, wait_and_solve_captcha")
    print("  solve_captcha(page)")
