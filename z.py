"""
PMOS 滑动验证码识别模块
核心原理：二维边缘轮廓形状匹配 - 专杀同水平线上1真多假缺口
"""

import numpy as np
import os

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False
    print("提示: opencv-python 未安装")

from constants import (
    CaptchaConstants,
    PathConstants
)


class CaptchaSolver:
    """验证码求解器 - 二维边缘轮廓匹配"""

    # 自适应补偿相关
    _global_offset_history = []  # 全局补偿历史
    _success_count = 0  # 成功次数
    _failure_count = 0  # 失败次数

    def __init__(self, debug=True, max_retry=None, enable_adaptive=True):
        self.has_opencv = HAS_OPENCV
        self.debug = debug
        self.debug_dir = PathConstants.DEBUG_CAPTCHA_DIR
        self.max_retry = max_retry or CaptchaConstants.MAX_RETRY
        self.enable_adaptive = enable_adaptive

        # 补偿参数（可调）
        self.base_offset_ratio = CaptchaConstants.BASE_OFFSET_RATIO
        self.adaptive_offset = 0  # 自适应补偿值
        self.offset_range = (CaptchaConstants.ADAPTIVE_OFFSET_MIN,
                            CaptchaConstants.ADAPTIVE_OFFSET_MAX)

        if debug:
            os.makedirs(self.debug_dir, exist_ok=True)

    def record_result(self, offset, success):
        """
        记录验证结果用于自适应学习
        Args:
            offset: 使用的补偿值
            success: 是否验证成功
        """
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
        """
        获取自适应补偿建议
        Returns: 优先级排序的补偿值列表
        """
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

    # ==================== 核心方法 ====================

    def get_slide_distance(self, bg_image, slider_image=None, slider_info=None):
        """
        专杀同一水平线上 1真多假 验证码
        核心原理：二维边缘形状交叉互相关匹配 + 阴影补偿
        
        算法背景：
        现代滑动验证码常在同一个水平线上设置多个相似的缺口（1个真实缺口 + 多个假缺口），
        仅靠传统的边缘检测无法准确识别。本算法采用二维轮廓匹配技术，通过对比滑块形状和背景
        边缘的相似度，精准定位真实缺口。
        
        技术路线：
        1. 提取滑块真实形状和Y坐标：从RGBA滑块图中提取透明通道，获取精确的拼图形状
        2. 截取背景图水平带：根据滑块Y坐标，只处理相关水平区域，减少干扰
        3. 边缘提取：使用Canny算法提取背景和滑块的边缘特征
        4. 形状匹配：使用OpenCV的模板匹配（TM_CCOEFF_NORMED）计算形状相似度
        5. 补偿修正：应用阴影补偿和初始边距补偿，修正匹配误差
        6. 结果验证：排除左侧初始坑位干扰，返回最佳匹配位置
        
        关键创新：
        - 二维轮廓匹配：不只是水平投影，而是完整的二维形状匹配
        - 透明通道利用：通过RGBA透明通道精确提取拼图形状，排除黑色边框干扰
        - 阴影补偿：缺口边缘的阴影会导致匹配位置偏左，需补偿8-12像素
        - 自适应学习：根据历史成功记录动态调整补偿值（enable_adaptive=True时）
        
        Args:
            bg_image: 背景图片（bytes或numpy数组），包含缺口的完整验证码图
            slider_image: 滑块图片（bytes或numpy数组，竖条RGBA格式，带透明通道），拼图块图像
            slider_info: 滑块信息字典（兼容旧版），包含滑块位置、尺寸等信息

        Returns:
            (gap_x, gap_y, confidence): 缺口在背景图中的X坐标、Y坐标和匹配置信度(0-1)
        """
        if not self.has_opencv:
            return (180, None, 0.5)

        bg_img = self._decode_image(bg_image)
        if bg_img is None:
            return (180, None, 0.5)

        h, w = bg_img.shape[:2]

        if self.debug:
            cv2.imwrite(f"{self.debug_dir}/01_original_bg.png", bg_img)

        # ==================== 第一步：提取滑块真实形状和 Y 坐标 ====================
        # 目的：从RGBA滑块图中提取精确的拼图形状和位置信息
        # 原理：RGBA图像中的透明通道（Alpha）可以准确区分拼图形状和背景
        #      通过分析透明像素的分布，确定拼图块的边界框（bounding box）
        # 输出：y_start, y_end（垂直范围），x_start（水平起始偏移），slider_rgba（裁剪后的RGBA图像）
        if slider_image is None:
            print("[二维匹配] 无滑块图片，回退到边缘检测")
            return self._fallback_edge_detection(bg_img, slider_info)

        y_start, y_end, x_start, slider_rgba = self._extract_slider_info_rgba(slider_image)

        if slider_rgba is None:
            print("[二维匹配] 提取滑块形状失败，回退到边缘检测")
            return self._fallback_edge_detection(bg_img, slider_info)

        # ==================== 第二步：截取背景图的 Y 轴水平带 ====================
        # 目的：缩小处理范围，提高匹配精度和计算效率
        # 原理：由于滑块只在特定的Y轴范围内移动，只需处理该水平带区域即可
        #      上下各加5像素容错，防止因坐标计算误差导致拼图块被截断
        # 容错：上下各加 5 像素
        strip_y_start = max(0, y_start - 5)
        strip_y_end = min(h, y_end + 5)
        bg_strip = bg_img[strip_y_start:strip_y_end, :]

        # ==================== 第三步：处理背景边缘 ====================
        # 目的：提取背景图的边缘特征，用于形状匹配
        # 处理流程：
        # 1. 灰度化：将BGR图像转换为灰度图，减少计算维度
        # 2. 高斯模糊：使用3x3高斯核平滑图像，消除噪声干扰
        # 3. Canny边缘检测：提取图像中的边缘特征，阈值为50-150
        bg_gray = cv2.cvtColor(bg_strip, cv2.COLOR_BGR2GRAY)
        bg_blur = cv2.GaussianBlur(bg_gray, (3, 3), 0)
        bg_edges = cv2.Canny(bg_blur, 50, 150)

        # ==================== 第四步：处理滑块边缘 ====================
        # 目的：提取滑块拼图块的边缘特征，用于与背景边缘进行匹配
        # 处理流程与背景边缘处理类似，但输入是RGBA图像（需要先转换为灰度）
        # 注意：slider_rgba是带有透明通道的裁剪后拼图块图像
        slider_gray = cv2.cvtColor(slider_rgba, cv2.COLOR_BGRA2GRAY)
        slider_blur = cv2.GaussianBlur(slider_gray, (3, 3), 0)
        slider_edges = cv2.Canny(slider_blur, 50, 150)

        # ==================== 第五步：消除滑块外围方形黑边的干扰 ====================
        # 目的：去除滑块图像中可能存在的黑色边框，只保留真正的拼图形状
        # 问题：许多滑块验证码会在拼图块周围添加黑色边框，干扰边缘检测
        # 解决方案：利用RGBA图像的透明通道（Alpha）创建掩膜，只保留非透明区域
        
        # 利用 Alpha 透明通道，把真正的"拼图曲线轮廓"筛出来
        # 原理：透明通道中，透明部分（alpha=0）为背景，不透明部分（alpha>0）为拼图形状
        alpha_channel = slider_rgba[:, :, 3]
        _, mask = cv2.threshold(alpha_channel, 10, 255, cv2.THRESH_BINARY)

        # 腐蚀掩膜，去除抗锯齿产生的脏边缘
        # 腐蚀操作可以消除边缘的半透明像素，确保掩膜边界清晰
        kernel = np.ones((2, 2), np.uint8)
        mask_eroded = cv2.erode(mask, kernel, iterations=1)

        # 运用掩膜，slider_edges 只剩下干干净净的拼图形状！
        # 按位与操作：只保留掩膜区域内的边缘像素
        slider_edges = cv2.bitwise_and(slider_edges, mask_eroded)

        # ==================== 第六步：二维交叉互相关匹配 ====================
        # 目的：在背景边缘图中查找与滑块边缘形状最匹配的位置
        # 算法：OpenCV的模板匹配（matchTemplate）算法
        # 匹配方法：TM_CCOEFF_NORMED（归一化相关系数匹配）
        #   - 优点：对光照变化、对比度变化高度鲁棒，只关注形状相似度
        #   - 原理：计算模板图像和源图像的归一化互相关系数，值越接近1表示匹配度越高
        # 输出：result矩阵，每个位置表示该处模板匹配的相似度得分
        result = cv2.matchTemplate(bg_edges, slider_edges, cv2.TM_CCOEFF_NORMED)

        # ==================== 第七步：排除左侧初始坑位 ====================
        # 目的：排除滑块初始位置（最左侧）的干扰匹配
        # 问题：滑块验证码在最左侧通常有一个初始坑位（滑块原本的位置），
        #      这个坑位的形状与滑块完全一致，会导致误匹配
        # 解决方案：将结果矩阵左侧区域（滑块宽度+20像素）的匹配得分设为-1（最低分）
        #   - 20像素的额外容差：防止因图像缩放或渲染导致的微小偏移
        ignore_width = slider_rgba.shape[1] + 20
        result[:, :ignore_width] = -1.0  # 把左侧匹配得分强行置为极低

        # ==================== 第八步：找出匹配度最高的位置 ====================
        # 目的：从匹配结果矩阵中找到相似度最高的位置
        # 函数：cv2.minMaxLoc() 查找矩阵中的最小值和最大值及其位置
        # 返回值：
        #   min_val: 最小匹配得分（最不相似）
        #   max_val: 最大匹配得分（最相似，范围[-1, 1]，越接近1匹配度越高）
        #   min_loc: 最小值位置（最不相似的位置）
        #   max_loc: 最大值位置（最相似的位置）
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        # 最佳匹配位置：max_loc[0]为X坐标，Y坐标取滑块垂直范围的中点
        best_x = max_loc[0]
        best_y = (y_start + y_end) // 2

        print(f"[二维轮廓匹配] 原始最佳X: {best_x}, 匹配得分: {max_val:.3f}")

        # ==================== 核心修复：距离补偿逻辑 ====================
        # 目的：修正匹配位置与实际缺口位置之间的系统误差
        # 误差来源分析：
        # 1. 阴影效应：缺口左侧通常有暗色阴影，边缘检测会捕捉到阴影的外边缘而非缺口真实边缘
        # 2. 腐蚀操作：第五步中的掩膜腐蚀使滑块轮廓"瘦"了一圈，导致匹配位置向左偏移
        # 3. 透明边距：滑块图像左侧可能有透明空白区域，影响实际位置计算
        
        # 补偿1：阴影与腐蚀补偿
        # 背景缺口的左边缘有暗色阴影，边缘检测会把阴影的最外层当成边缘
        # 另外腐蚀操作让滑块轮廓"瘦"了一圈，导致匹配位置偏左
        # 通常在 5 到 12 之间，根据实际效果调整
        shadow_compensation = 8

        # 补偿2：初始透明边距
        # 有些滑块图左侧有几像素的透明空白
        # 注意：此处的initial_padding仅用于信息输出，实际补偿已包含在shadow_compensation中
        initial_padding = x_start if x_start else 0

        # 最终实际应该滑动的像素距离
        # 匹配到的绝对坐标 + 阴影偏差补偿
        compensated_x = best_x + shadow_compensation

        # 确保在合理范围内
        # 下限：避开左侧初始坑位区域（ignore_width）
        # 上限：不超过背景图宽度减去滑块宽度
        compensated_x = max(ignore_width, min(compensated_x, w - slider_rgba.shape[1]))

        print(f"[二维轮廓匹配] 补偿后最终滑动距离: {compensated_x} (阴影补偿+{shadow_compensation}px, 初始边距={initial_padding}px)")
        # ================================================================

        # ==================== 第九步：保存调试图 ====================
        if self.debug:
            cv2.imwrite(f"{self.debug_dir}/02_bg_edges_strip.png", bg_edges)
            cv2.imwrite(f"{self.debug_dir}/03_slider_edges_clean.png", slider_edges)

            debug_img = bg_img.copy()
            # 画水平带范围（蓝框）
            cv2.rectangle(debug_img, (0, strip_y_start), (w, strip_y_end), (255, 0, 0), 1)
            # 画原始匹配位置（红框，偏左的）
            cv2.rectangle(debug_img, (best_x, strip_y_start), (best_x + slider_rgba.shape[1], strip_y_end), (0, 0, 255), 1)
            # 画补偿后位置（绿框，真正吻合的）
            cv2.rectangle(debug_img, (compensated_x, strip_y_start), (compensated_x + slider_rgba.shape[1], strip_y_end), (0, 255, 0), 2)
            cv2.circle(debug_img, (best_x, best_y), 5, (0, 0, 255), -1)
            cv2.imwrite(f"{self.debug_dir}/04_final_match.png", debug_img)

        return (compensated_x, best_y, max_val)

    def _extract_slider_info_rgba(self, slider_img_data):
        """
        解析竖向滑块图，提取精确的 Y 坐标和 X 坐标，并返回带有透明通道的真实滑块小图
        
        核心功能：从RGBA格式的滑块图像中提取拼图块的精确边界和形状
        技术原理：利用透明通道（Alpha）区分拼图形状和背景，通过非透明像素的分布确定边界框
        
        处理步骤：
        1. 图像解码：使用cv2.IMREAD_UNCHANGED模式读取RGBA四通道图像
        2. 透明通道提取：获取Alpha通道（第4通道）
        3. 边界检测：找到所有非透明像素（alpha > 0）的坐标，计算最小/最大X、Y值
        4. 图像裁剪：根据边界框裁剪出纯拼图形状（去除透明背景）
        5. 调试输出：可选保存可视化图像用于调试
        
        关键点：
        - 必须使用IMREAD_UNCHANGED保留透明通道，否则会丢失关键信息
        - 通过alpha > 0的阈值判断非透明像素，避免半透明像素的干扰
        - 返回的裁剪图像slider_rgba是去除了周围透明背景的纯拼图形状
        
        Returns:
            (y_start, y_end, x_start, slider_rgba) - Y坐标范围、X坐标初始偏移和滑块RGBA图
            - y_start, y_end: 拼图块在原始图像中的垂直范围（像素）
            - x_start: 拼图块在原始图像中的水平起始偏移（像素）
            - slider_rgba: 裁剪后的RGBA图像，只包含拼图形状，尺寸为(y_end-y_start+1) x (x_end-x_start+1)
        """
        if not self.has_opencv:
            return None, None, None, None

        # 必须使用 IMREAD_UNCHANGED 保留 RGBA 四个通道
        if isinstance(slider_img_data, bytes):
            arr = np.frombuffer(slider_img_data, np.uint8)
            slider_img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        elif isinstance(slider_img_data, str):
            slider_img = cv2.imread(slider_img_data, cv2.IMREAD_UNCHANGED)
        else:
            slider_img = slider_img_data

        if slider_img is None or len(slider_img.shape) < 3 or slider_img.shape[2] != 4:
            print(f"[滑块提取] 警告：滑块不是RGBA格式！shape={slider_img.shape if slider_img is not None else None}")
            return None, None, None, None

        alpha_channel = slider_img[:, :, 3]

        # 找到非透明像素的边界
        y_coords, x_coords = np.where(alpha_channel > 0)
        if len(y_coords) == 0:
            return None, None, None, None

        y_start, y_end = np.min(y_coords), np.max(y_coords)
        x_start, x_end = np.min(x_coords), np.max(x_coords)

        # 截取真实的滑块拼图（包含 RGBA）
        cropped_slider_rgba = slider_img[y_start:y_end+1, x_start:x_end+1]

        print(f"[滑块提取] 原始尺寸: {slider_img.shape}, 滑块范围: Y=[{y_start},{y_end}], X偏移={x_start}, 切图尺寸: {cropped_slider_rgba.shape}")

        if self.debug:
            # 保存可视化图（透明背景转白色）
            display_img = cropped_slider_rgba.copy()
            white_bg = np.ones((display_img.shape[0], display_img.shape[1], 3), dtype=np.uint8) * 255
            mask = display_img[:, :, 3] > 0
            white_bg[mask] = display_img[mask, :3]
            cv2.imwrite(f"{self.debug_dir}/01_slider_cropped.png", white_bg)

        return y_start, y_end, x_start, cropped_slider_rgba

    def _fallback_edge_detection(self, bg_img, slider_info):
        """
        备用方法：当没有滑块图片时，使用边缘检测
        
        应用场景：当无法获取滑块拼图块图像时，回退到传统的边缘检测方法
        技术原理：基于Canny边缘检测和水平投影分析，识别缺口位置
        
        处理流程：
        1. 确定Y轴范围：优先使用slider_info中的Y坐标，否则从背景图左侧区域检测
        2. 边缘检测：对背景图进行灰度化、高斯模糊、Canny边缘提取
        3. Y轴掩膜：根据确定的Y范围创建掩膜，只处理相关水平带
        4. 缺口检测：通过边缘的水平投影分析，寻找缺口位置（边缘密度较低的区域）
        5. 结果返回：返回检测到的缺口X坐标、Y坐标和置信度
        
        局限性：
        - 无法处理同一水平线上的多个假缺口（1真多假场景）
        - 对图像噪声和复杂背景较为敏感
        - 准确率低于基于滑块形状的二维匹配方法
        
        参数：
            bg_img: 背景图像（numpy数组）
            slider_info: 滑块信息字典，可能包含Y坐标信息
        
        返回：
            (gap_x, gap_y, confidence): 缺口位置和置信度
        """
        h, w = bg_img.shape[:2]

        # 尝试获取滑块Y坐标
        y_start, y_end = 0, h
        slider_width = 60

        if slider_info:
            if 'y' in slider_info and slider_info['y'] > 0:
                slider_y = slider_info['y']
                y_start = max(0, int(slider_y) - 10)
                y_end = min(h, int(slider_y) + slider_width + 10)
                print(f"[备用检测] 从slider_info提取Y: {slider_y}")

        if y_start == 0 and y_end == h:
            y_start, y_end = self._detect_slider_y_from_bg(bg_img, w, h)

        # 边缘检测
        gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)

        # Y轴掩膜
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
        """
        从背景图检测滑块Y轴范围
        
        核心算法：基于垂直边缘密度分析的滑块位置检测
        技术原理：滑块通常位于图像左侧，其垂直边缘密度较高，通过分析左侧区域的边缘分布确定Y坐标
        
        处理步骤：
        1. 区域选择：截取图像左侧区域（宽度为min(80, w//4)），这是滑块最可能出现的区域
        2. 边缘检测：对左侧区域进行灰度化、Canny边缘提取
        3. 垂直投影：计算每行的边缘像素总和（row_edges = np.sum(edges, axis=1)）
        4. 平滑处理：使用5像素的均值滤波器平滑投影曲线，减少噪声影响
        5. 峰值检测：找到平滑后曲线的最大值位置，作为滑块的垂直中心
        6. 范围确定：以峰值位置为中心，上下扩展一定范围（通常±30-40像素）
        
        应用场景：当无法从滑块图像或滑块信息中获取Y坐标时，使用此方法估计滑块垂直位置
        
        参数：
            img: 背景图像（numpy数组，BGR格式）
            w: 图像宽度（像素）
            h: 图像高度（像素）
        
        返回：
            (y_start, y_end): 滑块估计的垂直范围（起始Y坐标，结束Y坐标）
        """
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
        """
        通过边缘的水平投影检测缺口的X位置
        
        核心算法：基于边缘密度分析的峰值检测方法
        技术原理：计算每列像素的边缘密度（边缘像素数量），缺口位置通常表现为边缘密度较高的峰值
        
        处理步骤：
        1. 水平投影：计算每列的边缘像素总和（col_edges = np.sum(edges, axis=0)）
        2. 平滑处理：使用卷积核（3像素和7像素）对投影曲线进行双重平滑，减少噪声干扰
        3. 基线计算：在有效区域（start_x到end_x）计算边缘密度的均值和标准差
        4. 峰值检测：遍历有效区域，寻找超过阈值（均值+0.8*标准差）的局部最大值
        5. 评分排序：根据峰值强度和锐度对候选位置进行评分，选择最佳位置
        6. 补偿修正：应用基础补偿和自适应补偿，修正检测位置
        
        关键优化：
        - 双重平滑：结合短期（3像素）和长期（7像素）平滑，平衡噪声抑制和细节保留
        - 自适应阈值：基于局部统计特性动态调整峰值检测阈值
        - 锐度评估：通过峰值与周围区域的对比度评估缺口质量
        - 补偿机制：根据滑块宽度比例和自适应学习结果进行位置补偿
        
        参数：
            edges: 边缘图像（二值图，边缘像素为255）
            h: 图像高度（像素）
            w: 图像宽度（像素）
            slider_width: 滑块宽度（像素），用于补偿计算
        
        返回：
            int or None: 检测到的缺口X坐标，如果未找到则返回None
        """
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

        min_score = (base_std * 2) if base_std > 10 else 50
        if best_score < min_score:
            print(f"[边缘检测X] 得分不足: {best_score:.1f}")

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
