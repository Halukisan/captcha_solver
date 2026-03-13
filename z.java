import org.opencv.core.*;
import org.opencv.imgcodecs.Imgcodecs;
import org.opencv.imgproc.Imgproc;
import org.openqa.selenium.*;
import org.openqa.selenium.interactions.Actions;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.WebDriverWait;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.URL;
import java.util.*;
import java.util.Base64;
import java.util.concurrent.ThreadLocalRandom;

/**
 * PMOS 滑动验证码处理模块 (Java版本)
 * 功能：获取验证码 + 滑动滑块完整流程
 *
 * 核心原理：二维边缘轮廓形状匹配 - 专杀同水平线上1真多假缺口
 *
 * 使用示例:
 * <pre>
 *     CaptchaHandler handler = new CaptchaHandler(true);
 *     boolean success = handler.solveCaptcha(driver, null);
 * </pre>
 *
 * 依赖:
 * - OpenCV Java SDK
 * - Selenium WebDriver
 *
 * @author PMOS Team
 * @version 1.0
 */
public class z {

    // ==================== 常量定义 ====================

    /**
     * 验证码相关常量
     */
    public static class CaptchaConstants {
        public static final int DEFAULT_SLIDER_WIDTH = 60;
        public static final int SLIDER_WIDTH_MIN = 40;
        public static final int SLIDER_WIDTH_MAX = 100;
        public static final int GAP_SEARCH_START_X = 50;
        public static final double GAP_SEARCH_END_RATIO = 0.7;
        public static final int GAP_SEARCH_MAX_X = 300;
        public static final double BASE_OFFSET_RATIO = 0.12;
        public static final int ADAPTIVE_OFFSET_MIN = -15;
        public static final int ADAPTIVE_OFFSET_MAX = 10;
        public static final int MAX_RETRY = 6;
        public static final double CAPTCHA_CHECK_WAIT = 1.5;
        public static final double CAPTCHA_REFRESH_WAIT = 0.5;
    }

    /**
     * 拖动轨迹相关常量
     */
    public static class DragConstants {
        public static final int DELAY_START_MIN = 8;
        public static final int DELAY_START_MAX = 20;
        public static final int DELAY_MIDDLE_MIN = 6;
        public static final int DELAY_MIDDLE_MAX = 15;
        public static final int DELAY_END_MIN = 20;
        public static final int DELAY_END_MAX = 50;
        public static final int RANDOM_OFFSET_MIN = -2;
        public static final int RANDOM_OFFSET_MAX = 2;
        public static final int Y_JITTER_MIN = -1;
        public static final int Y_JITTER_MAX = 1;
    }

    // ==================== 验证码识别核心类 ====================

    /**
     * 验证码求解器 - 二维边缘轮廓匹配
     */
    public static class CaptchaSolver {
        // 自适应补偿相关（类变量）
        private static List<OffsetRecord> globalOffsetHistory = new ArrayList<>();
        private static int successCount = 0;
        private static int failureCount = 0;

        private boolean hasOpenCV;
        private boolean debug;
        private String debugDir;
        private int maxRetry;
        private boolean enableAdaptive;
        private double baseOffsetRatio;
        private double adaptiveOffset;
        private int[] offsetRange;

        public CaptchaSolver() {
            this(true);
        }

        public CaptchaSolver(boolean debug) {
            this(debug, null, true);
        }

        public CaptchaSolver(boolean debug, Integer maxRetry, boolean enableAdaptive) {
            try {
                // 尝试加载OpenCV
                System.loadLibrary(Core.NATIVE_LIBRARY_NAME);
                this.hasOpenCV = true;
            } catch (UnsatisfiedLinkError e) {
                System.out.println("提示: OpenCV 未正确加载");
                this.hasOpenCV = false;
            }

            this.debug = debug;
            this.debugDir = "debug_captcha";
            this.maxRetry = maxRetry != null ? maxRetry : CaptchaConstants.MAX_RETRY;
            this.enableAdaptive = enableAdaptive;
            this.baseOffsetRatio = CaptchaConstants.BASE_OFFSET_RATIO;
            this.adaptiveOffset = 0;
            this.offsetRange = new int[]{
                CaptchaConstants.ADAPTIVE_OFFSET_MIN,
                CaptchaConstants.ADAPTIVE_OFFSET_MAX
            };

            if (debug) {
                new java.io.File(debugDir).mkdirs();
            }
        }

        /**
         * 记录验证结果用于自适应学习
         */
        public void recordResult(int offset, boolean success) {
            if (!enableAdaptive) return;

            globalOffsetHistory.add(new OffsetRecord(offset, success));

            if (success) {
                successCount++;
                if (globalOffsetHistory.size() > 10) {
                    globalOffsetHistory.removeIf(r -> !r.success && globalOffsetHistory.size() >= 20);
                }
            } else {
                failureCount++;
            }

            updateAdaptiveOffset();
        }

        private void updateAdaptiveOffset() {
            List<OffsetRecord> successRecords = new ArrayList<>();
            for (OffsetRecord r : globalOffsetHistory) {
                if (r.success) successRecords.add(r);
            }

            if (successRecords.size() >= 3) {
                double weightedSum = 0;
                double totalWeight = 0;

                for (int i = 0; i < successRecords.size(); i++) {
                    double weight = Math.pow(0.5, successRecords.size() - 1 - i);
                    weightedSum += successRecords.get(i).offset * weight;
                    totalWeight += weight;
                }

                double newOffset = weightedSum / totalWeight;
                newOffset = Math.max(offsetRange[0], Math.min(offsetRange[1], newOffset));

                if (Math.abs(newOffset - adaptiveOffset) > 1) {
                    System.out.printf("[自适应] 补偿值调整: %.1f -> %.1fpx%n",
                        adaptiveOffset, newOffset);
                }

                adaptiveOffset = newOffset;
            }
        }

        /**
         * 获取自适应补偿建议
         */
        public List<Integer> getAdaptiveSuggestions(double baseDistance) {
            List<Integer> suggestions = new ArrayList<>();

            if (Math.abs(adaptiveOffset) > 0.5) {
                suggestions.add((int) adaptiveOffset);
            }

            Set<Integer> added = new HashSet<>(suggestions);
            for (OffsetRecord r : globalOffsetHistory) {
                if (r.success && !added.contains(r.offset)) {
                    suggestions.add(r.offset);
                    added.add(r.offset);
                }
            }

            int[] commonOffsets = {0, -2, 2, -4, 4, -6, 6, -8, 8, -10, 10};
            for (int offset : commonOffsets) {
                if (!added.contains(offset)) {
                    suggestions.add(offset);
                    added.add(offset);
                }
            }

            return suggestions.subList(0, Math.min(15, suggestions.size()));
        }

        public static void resetAdaptive() {
            globalOffsetHistory.clear();
            successCount = 0;
            failureCount = 0;
        }

        /**
         * 滑块距离识别结果
         */
        public static class SlideDistanceResult {
            public double gapX;
            public Double gapY;
            public double confidence;

            public SlideDistanceResult(double gapX, Double gapY, double confidence) {
                this.gapX = gapX;
                this.gapY = gapY;
                this.confidence = confidence;
            }
        }

        /**
         * 核心识别方法：专杀同一水平线上1真多假验证码
         * 核心原理：二维边缘形状交叉互相关匹配 + 阴影补偿
         */
        public SlideDistanceResult getSlideDistance(byte[] bgImage, byte[] sliderImage, SliderInfo sliderInfo) {
            if (!hasOpenCV) {
                return new SlideDistanceResult(180, null, 0.5);
            }

            Mat bgImg = decodeImage(bgImage);
            if (bgImg == null || bgImg.empty()) {
                return new SlideDistanceResult(180, null, 0.5);
            }

            int h = bgImg.rows();
            int w = bgImg.cols();

            if (debug) {
                Imgcodecs.imwrite(debugDir + "/01_original_bg.png", bgImg);
            }

            // 提取滑块真实形状和Y坐标
            if (sliderImage == null) {
                System.out.println("[二维匹配] 无滑块图片，回退到边缘检测");
                return fallbackEdgeDetection(bgImg, sliderInfo);
            }

            SliderInfoRGBA rgba = extractSliderInfoRGBA(sliderImage);
            if (rgba == null) {
                System.out.println("[二维匹配] 提取滑块形状失败，回退到边缘检测");
                return fallbackEdgeDetection(bgImg, sliderInfo);
            }

            // 截取背景图的Y轴水平带
            int stripYStart = Math.max(0, rgba.yStart - 5);
            int stripYEnd = Math.min(h, rgba.yEnd + 5);
            Rect stripRect = new Rect(0, stripYStart, w, stripYEnd - stripYStart);
            Mat bgStrip = new Mat(bgImg, stripRect);

            // 处理背景边缘
            Mat bgGray = new Mat();
            Imgproc.cvtColor(bgStrip, bgGray, Imgproc.COLOR_BGR2GRAY);
            Mat bgBlur = new Mat();
            Imgproc.GaussianBlur(bgGray, bgBlur, new Size(3, 3), 0);
            Mat bgEdges = new Mat();
            Imgproc.Canny(bgBlur, bgEdges, 50, 150);

            // 处理滑块边缘
            Mat sliderGray = new Mat();
            Imgproc.cvtColor(rgba.sliderRgba, sliderGray, Imgproc.COLOR_BGRA2GRAY);
            Mat sliderBlur = new Mat();
            Imgproc.GaussianBlur(sliderGray, sliderBlur, new Size(3, 3), 0);
            Mat sliderEdges = new Mat();
            Imgproc.Canny(sliderBlur, sliderEdges, 50, 150);

            // 消除滑块外围方形黑边的干扰
            List<Mat> channels = new ArrayList<>();
            Core.split(rgba.sliderRgba, channels);
            Mat alphaChannel = channels.get(3);
            Mat mask = new Mat();
            Imgproc.threshold(alphaChannel, mask, 10, 255, Imgproc.THRESH_BINARY);

            Mat kernel = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, new Size(2, 2));
            Mat maskEroded = new Mat();
            Imgproc.erode(mask, maskEroded, kernel, new Point(-1, -1), 1);

            Core.bitwise_and(sliderEdges, maskEroded, sliderEdges);

            // 二维交叉互相关匹配
            Mat result = new Mat();
            Imgproc.matchTemplate(bgEdges, sliderEdges, result, Imgproc.TM_CCOEFF_NORMED);

            // 排除左侧初始坑位
            int ignoreWidth = rgba.sliderRgba.cols() + 20;
            for (int y = 0; y < result.rows(); y++) {
                for (int x = 0; x < Math.min(ignoreWidth, result.cols()); x++) {
                    result.put(y, x, -1.0);
                }
            }

            // 找出匹配度最高的位置
            Core.MinMaxLocResult mmr = Core.minMaxLoc(result);
            double bestX = mmr.maxLoc.x;
            double bestY = (rgba.yStart + rgba.yEnd) / 2.0;
            double maxVal = mmr.maxVal;

            System.out.printf("[二维轮廓匹配] 原始最佳X: %.0f, 匹配得分: %.3f%n", bestX, maxVal);

            // 距离补偿逻辑
            int shadowCompensation = 8;
            int initialPadding = rgba.xStart;
            double compensatedX = bestX + shadowCompensation;
            compensatedX = Math.max(ignoreWidth, Math.min(compensatedX, w - rgba.sliderRgba.cols()));

            System.out.printf("[二维轮廓匹配] 补偿后最终滑动距离: %.0f (阴影补偿+%dpx)%n",
                compensatedX, shadowCompensation);

            // 保存调试图
            if (debug) {
                Imgcodecs.imwrite(debugDir + "/02_bg_edges_strip.png", bgEdges);
                Imgcodecs.imwrite(debugDir + "/03_slider_edges_clean.png", sliderEdges);

                Mat debugImg = bgImg.clone();
                Imgproc.rectangle(debugImg,
                    new Point(0, stripYStart),
                    new Point(w, stripYEnd),
                    new Scalar(255, 0, 0), 1);
                Imgproc.rectangle(debugImg,
                    new Point(bestX, stripYStart),
                    new Point(bestX + rgba.sliderRgba.cols(), stripYEnd),
                    new Scalar(0, 0, 255), 1);
                Imgproc.rectangle(debugImg,
                    new Point(compensatedX, stripYStart),
                    new Point(compensatedX + rgba.sliderRgba.cols(), stripYEnd),
                    new Scalar(0, 255, 0), 2);
                Imgproc.circle(debugImg, new Point(bestX, bestY), 5, new Scalar(0, 0, 255), -1);
                Imgcodecs.imwrite(debugDir + "/04_final_match.png", debugImg);
            }

            return new SlideDistanceResult(compensatedX, bestY, maxVal);
        }

        /**
         * 滑块RGBA信息
         */
        public static class SliderInfoRGBA {
            public int yStart;
            public int yEnd;
            public int xStart;
            public Mat sliderRgba;

            public SliderInfoRGBA(int yStart, int yEnd, int xStart, Mat sliderRgba) {
                this.yStart = yStart;
                this.yEnd = yEnd;
                this.xStart = xStart;
                this.sliderRgba = sliderRgba;
            }
        }

        private SliderInfoRGBA extractSliderInfoRGBA(byte[] sliderImgData) {
            if (!hasOpenCV) return null;

            MatOfByte mob = new MatOfByte(sliderImgData);
            Mat sliderImg = Imgcodecs.imdecode(mob, Imgcodecs.IMREAD_UNCHANGED);

            if (sliderImg == null || sliderImg.empty() || sliderImg.channels() != 4) {
                return null;
            }

            List<Mat> channels = new ArrayList<>();
            Core.split(sliderImg, channels);
            Mat alphaChannel = channels.get(3);

            List<Point> nonZeroPoints = new ArrayList<>();
            byte[] alphaData = new byte[(int) alphaChannel.total()];
            alphaChannel.get(0, 0, alphaData);

            for (int y = 0; y < alphaChannel.rows(); y++) {
                for (int x = 0; x < alphaChannel.cols(); x++) {
                    if ((alphaData[y * alphaChannel.cols() + x] & 0xFF) > 0) {
                        nonZeroPoints.add(new Point(x, y));
                    }
                }
            }

            if (nonZeroPoints.isEmpty()) return null;

            int yStart = Integer.MAX_VALUE, yEnd = Integer.MIN_VALUE;
            int xStart = Integer.MAX_VALUE, xEnd = Integer.MIN_VALUE;

            for (Point p : nonZeroPoints) {
                yStart = Math.min(yStart, (int) p.y);
                yEnd = Math.max(yEnd, (int) p.y);
                xStart = Math.min(xStart, (int) p.x);
                xEnd = Math.max(xEnd, (int) p.x);
            }

            Rect cropRect = new Rect(xStart, yStart, xEnd - xStart + 1, yEnd - yStart + 1);
            Mat croppedSliderRgba = new Mat(sliderImg, cropRect);

            System.out.printf("[滑块提取] 原始尺寸: %dx%d, 滑块范围: Y=[%d,%d], X偏移=%d%n",
                sliderImg.cols(), sliderImg.rows(), yStart, yEnd, xStart);

            if (debug) {
                Mat displayImg = croppedSliderRgba.clone();
                Mat whiteBg = new Mat(displayImg.rows(), displayImg.cols(), CvType.CV_8UC3, new Scalar(255, 255, 255));
                Mat mask = new Mat();
                List<Mat> displayChannels = new ArrayList<>();
                Core.split(displayImg, displayChannels);
                displayChannels.get(3).copyTo(mask);
                displayImg.copyTo(whiteBg, mask);
                Imgcodecs.imwrite(debugDir + "/01_slider_cropped.png", whiteBg);
            }

            return new SliderInfoRGBA(yStart, yEnd, xStart, croppedSliderRgba);
        }

        private SlideDistanceResult fallbackEdgeDetection(Mat bgImg, SliderInfo sliderInfo) {
            int h = bgImg.rows();
            int w = bgImg.cols();
            int yStart = 0, yEnd = h;
            int sliderWidth = 60;

            if (sliderInfo != null && sliderInfo.y > 0) {
                int sliderY = sliderInfo.y;
                yStart = Math.max(0, sliderY - 10);
                yEnd = Math.min(h, sliderY + sliderWidth + 10);
            }

            if (yStart == 0 && yEnd == h) {
                int[] detectedY = detectSliderYFromBg(bgImg, w, h);
                yStart = detectedY[0];
                yEnd = detectedY[1];
            }

            // 边缘检测
            Mat gray = new Mat();
            Imgproc.cvtColor(bgImg, gray, Imgproc.COLOR_BGR2GRAY);
            Mat blurred = new Mat();
            Imgproc.GaussianBlur(gray, blurred, new Size(5, 5), 0);
            Mat edges = new Mat();
            Imgproc.Canny(blurred, edges, 50, 150);

            if (yEnd > yStart && (yEnd - yStart) < h * 0.8) {
                Mat mask = Mat.zeros(edges.size(), CvType.CV_8UC1);
                int maskYStart = Math.max(0, yStart - 5);
                int maskYEnd = Math.min(h, yEnd + 5);
                Rect roi = new Rect(0, maskYStart, w, maskYEnd - maskYStart);
                mask.put(maskYStart, 0, new byte[w * (maskYEnd - maskYStart)]);
                Core.bitwise_and(edges, mask, edges);
            }

            Integer gapX = detectGapXByEdge(edges, h, w, sliderWidth);

            if (gapX != null) {
                int gapY = (yStart + yEnd) / 2;
                return new SlideDistanceResult(gapX, (double) gapY, 0.7);
            }

            return new SlideDistanceResult(Math.min(w - 50, 200), h / 2.0, 0.3);
        }

        private int[] detectSliderYFromBg(Mat img, int w, int h) {
            int leftRoiWidth = Math.min(80, w / 4);
            Rect leftRoi = new Rect(0, 0, leftRoiWidth, h);
            Mat leftRoiMat = new Mat(img, leftRoi);

            Mat gray = new Mat();
            Imgproc.cvtColor(leftRoiMat, gray, Imgproc.COLOR_BGR2GRAY);
            Mat edges = new Mat();
            Imgproc.Canny(gray, edges, 50, 150);

            Mat rowEdges = new Mat();
            Core.reduce(edges, rowEdges, 1, Core.REDUCE_SUM, CvType.CV_32F);

            float[] rowEdgesData = new float[(int) rowEdges.total()];
            rowEdges.get(0, 0, rowEdgesData);

            if (rowEdgesData.length > 0) {
                float maxVal = 0;
                int maxIdx = 0;
                for (int i = 0; i < rowEdgesData.length; i++) {
                    if (rowEdgesData[i] > maxVal) {
                        maxVal = rowEdgesData[i];
                        maxIdx = i;
                    }
                }

                int centerY = maxIdx;
                int halfH = 30;
                return new int[]{
                    Math.max(0, centerY - halfH - 10),
                    Math.min(h, centerY + halfH + 10)
                };
            }

            return new int[]{0, h};
        }

        private Integer detectGapXByEdge(Mat edges, int h, int w, int sliderWidth) {
            Mat colEdges = new Mat();
            Core.reduce(edges, colEdges, 0, Core.REDUCE_SUM, CvType.CV_32F);

            float[] colEdgesData = new float[(int) colEdges.total()];
            colEdges.get(0, 0, colEdgesData);

            // 平滑处理
            float[] colEdgesSmooth = new float[colEdgesData.length];
            for (int i = 0; i < colEdgesData.length; i++) {
                float sum1 = 0, sum2 = 0;
                int count1 = 0, count2 = 0;
                for (int j = -1; j <= 1 && i + j >= 0 && i + j < colEdgesData.length; j++) {
                    sum1 += colEdgesData[i + j];
                    count1++;
                }
                for (int j = -3; j <= 3 && i + j >= 0 && i + j < colEdgesData.length; j++) {
                    sum2 += colEdgesData[i + j];
                    count2++;
                }
                colEdgesSmooth[i] = (sum1 / count1) * 0.6f + (sum2 / count2) * 0.4f;
            }

            int startX = 50;
            int endX = Math.min(w - 50, Math.max(300, (int) (w * 0.7)));

            // 计算基准值
            float sum = 0;
            int count = 0;
            for (int x = startX; x < endX; x++) {
                sum += colEdgesSmooth[x];
                count++;
            }
            float baseMean = sum / count;

            float variance = 0;
            for (int x = startX; x < endX; x++) {
                variance += (colEdgesSmooth[x] - baseMean) * (colEdgesSmooth[x] - baseMean);
            }
            float baseStd = (float) Math.sqrt(variance / count);

            List<double[]> candidates = new ArrayList<>();

            for (int x = startX; x < endX; x++) {
                float val = colEdgesSmooth[x];
                float threshold = baseMean + baseStd * 0.8f;
                if (val < threshold) continue;

                boolean isPeak = true;
                int peakRange = 7;
                for (int dx = -peakRange; dx <= peakRange; dx++) {
                    if (dx == 0) continue;
                    int nx = x + dx;
                    if (nx >= 0 && nx < w && colEdgesSmooth[nx] > val) {
                        isPeak = false;
                        break;
                    }
                }

                if (isPeak) {
                    float score = val - baseMean;
                    float neighborhoodMean = 0;
                    int neighborCount = 0;
                    for (int nx = Math.max(0, x - 5); nx <= Math.min(w - 1, x + 5); nx++) {
                        neighborhoodMean += colEdgesSmooth[nx];
                        neighborCount++;
                    }
                    neighborhoodMean /= neighborCount;
                    float sharpness = val / (neighborhoodMean + 1);
                    candidates.add(new double[]{x, score * sharpness});
                }
            }

            if (candidates.isEmpty()) return null;

            candidates.sort((a, b) -> Double.compare(b[1], a[1]));
            double[] best = candidates.get(0);
            int bestX = (int) best[0];

            int baseOffset = (int) (sliderWidth * baseOffsetRatio);
            int totalOffset = (int) (baseOffset + adaptiveOffset);

            int compensatedX = bestX - totalOffset;
            compensatedX = Math.max(startX, Math.min(compensatedX, endX));

            System.out.printf("[边缘检测X] 峰值=%d, 补偿后=%d%n", bestX, compensatedX);

            return compensatedX;
        }

        private Mat decodeImage(byte[] image) {
            if (image == null || image.length == 0) return null;

            MatOfByte mob = new MatOfByte(image);
            return Imgcodecs.imdecode(mob, Imgcodecs.IMREAD_COLOR);
        }
    }

    /**
     * 滑块信息
     */
    public static class SliderInfo {
        public int x;
        public int y;
        public int width;
        public int height;

        public SliderInfo(int x, int y, int width, int height) {
            this.x = x;
            this.y = y;
            this.width = width;
            this.height = height;
        }
    }

    /**
     * 补偿记录
     */
    private static class OffsetRecord {
        int offset;
        boolean success;

        OffsetRecord(int offset, boolean success) {
            this.offset = offset;
            this.success = success;
        }
    }

    /**
     * 验证码位置信息
     */
    public static class CaptchaPositionInfo {
        public Rectangle sliderBox;
        public Rectangle trackBox;
        public Rectangle containerBox;
        public byte[] bgImage;
        public byte[] blockImage;
        public ImageOffset imgOffset;

        public CaptchaPositionInfo(Rectangle sliderBox, Rectangle trackBox,
                                   Rectangle containerBox, byte[] bgImage,
                                   byte[] blockImage, ImageOffset imgOffset) {
            this.sliderBox = sliderBox;
            this.trackBox = trackBox;
            this.containerBox = containerBox;
            this.bgImage = bgImage;
            this.blockImage = blockImage;
            this.imgOffset = imgOffset;
        }
    }

    /**
     * 图片偏移信息
     */
    public static class ImageOffset {
        public int x;
        public int y;
        public int width;
        public int height;

        public ImageOffset(int x, int y, int width, int height) {
            this.x = x;
            this.y = y;
            this.width = width;
            this.height = height;
        }
    }

    /**
     * 轨迹点
     */
    public static class TrackPoint {
        public int x;
        public double delay;

        public TrackPoint(int x, double delay) {
            this.x = x;
            this.delay = delay;
        }
    }

    // ==================== 验证码处理主类 ====================

    /**
     * 验证码处理器 - 完整的获取验证码和滑动流程
     */
    public static class CaptchaHandler {
        private boolean debug;
        private CaptchaSolver captchaSolver;

        public CaptchaHandler() {
            this(true);
        }

        public CaptchaHandler(boolean debug) {
            this(debug, null);
        }

        public CaptchaHandler(boolean debug, Integer maxRetry) {
            this.debug = debug;
            this.captchaSolver = new CaptchaSolver(debug, maxRetry, true);
        }

        /**
         * 查找滑块元素
         */
        public WebElement findSliderElement(WebDriver driver) {
            String[][] captchaConfigs = {
                {".verify-move-block", ".verify-left-bar", ".verify-bar-area"},
                {"#nc_1__scale_text", ".nc_1__scale_text", ".yidun_intelligence"},
                {"[class*='slider-btn']", "[class*='slider-track']", "[class*='captcha']"},
            };

            for (String[] config : captchaConfigs) {
                try {
                    List<WebElement> checkElements = driver.findElements(By.cssSelector(config[2]));
                    if (!checkElements.isEmpty()) {
                        WebDriverWait wait = new WebDriverWait(driver, java.time.Duration.ofSeconds(1));
                        WebElement slider = wait.until(ExpectedConditions.presenceOfElementLocated(By.cssSelector(config[0])));
                        if (slider != null && slider.isDisplayed()) {
                            return slider;
                        }
                    }
                } catch (Exception e) {
                    // 继续尝试下一个配置
                }
            }

            // 模糊匹配
            String[] fuzzySelectors = {"[class*='slider-btn']", "[class*='slide-btn']", ".yidun_slider__icon"};
            for (String selector : fuzzySelectors) {
                try {
                    List<WebElement> elements = driver.findElements(By.cssSelector(selector));
                    for (WebElement elem : elements) {
                        if (elem.isDisplayed()) {
                            return elem;
                        }
                    }
                } catch (Exception e) {
                    // 继续尝试
                }
            }

            return null;
        }

        /**
         * 获取验证码图片
         */
        public CaptchaPositionInfo getCaptchaImages(WebDriver driver, WebElement container) {
            byte[] bgImage = null;
            byte[] blockImage = null;
            ImageOffset imgOffset = null;

            try {
                // 使用JavaScript获取图片信息
                JavascriptExecutor js = (JavascriptExecutor) driver;

                @SuppressWarnings("unchecked")
                Map<String, Object> result = (Map<String, Object>) js.executeScript(
                    "(() => {" +
                    "  const allImages = [];" +
                    "  const selectors = [" +
                    "    '.verify-img-panel img', '.verify-img-out img', '.verify-area img'," +
                    "    '.verify-img-panel canvas', '.verify-img-out canvas', '.verify-area canvas'" +
                    "  ];" +
                    "  for (const sel of selectors) {" +
                    "    const elems = document.querySelectorAll(sel);" +
                    "    elems.forEach((el) => {" +
                    "      if (el.offsetParent !== null) {" +
                    "        const rect = el.getBoundingClientRect();" +
                    "        let src = null;" +
                    "        if (el.tagName === 'CANVAS') {" +
                    "          try { src = el.toDataURL('image/png'); } catch(e) {}" +
                    "        } else if (el.tagName === 'IMG') {" +
                    "          src = el.src;" +
                    "        }" +
                    "        if (src) {" +
                    "          const container = document.querySelector('.verify-img-panel, .verify-img-out, .verify-area');" +
                    "          let offsetX = 0, offsetY = 0;" +
                    "          if (container) {" +
                    "            const containerRect = container.getBoundingClientRect();" +
                    "            offsetX = rect.left - containerRect.left;" +
                    "            offsetY = rect.top - containerRect.top;" +
                    "          }" +
                    "          allImages.push({" +
                    "            src: src, width: Math.round(rect.width), height: Math.round(rect.height)," +
                    "            area: Math.round(rect.width * rect.height)," +
                    "            offsetX: Math.round(offsetX), offsetY: Math.round(offsetY)" +
                    "          });" +
                    "        }" +
                    "      }" +
                    "    });" +
                    "  }" +
                    "  allImages.sort((a, b) => b.area - a.area);" +
                    "  return { count: allImages.length, images: allImages };" +
                    "})()"
                );

                if (result != null) {
                    @SuppressWarnings("unchecked")
                    List<Map<String, Object>> images = (List<Map<String, Object>>) result.get("images");
                    if (images != null && !images.isEmpty()) {
                        Map<String, Object> imgInfo = images.get(0);
                        String src = (String) imgInfo.get("src");
                        if (src != null) {
                            bgImage = fetchImageData(driver, src);
                            imgOffset = new ImageOffset(
                                ((Number) imgInfo.getOrDefault("offsetX", 0)).intValue(),
                                ((Number) imgInfo.getOrDefault("offsetY", 0)).intValue(),
                                ((Number) imgInfo.getOrDefault("width", 0)).intValue(),
                                ((Number) imgInfo.getOrDefault("height", 0)).intValue()
                            );
                        }
                    }
                }
            } catch (Exception e) {
                System.out.println("[验证码] 获取背景图出错: " + e.getMessage());
            }

            // 获取滑块拼图
            try {
                JavascriptExecutor js = (JavascriptExecutor) driver;
                @SuppressWarnings("unchecked")
                Map<String, Object> sliderBg = (Map<String, Object>) js.executeScript(
                    "(() => {" +
                    "  const slider = document.querySelector('.verify-move-block');" +
                    "  if (!slider) return null;" +
                    "  const style = window.getComputedStyle(slider);" +
                    "  const bgImage = style.backgroundImage;" +
                    "  if (bgImage && bgImage !== 'none') {" +
                    "    const match = bgImage.match(/url\\(['\"]?([^'\"]+)['\"]?\\)/);" +
                    "    if (match) return { src: match[1], method: 'background' };" +
                    "  }" +
                    "  const img = slider.querySelector('img');" +
                    "  if (img && img.src) return { src: img.src, method: 'child_img' };" +
                    "  return null;" +
                    "})()"
                );

                if (sliderBg != null) {
                    String src = (String) sliderBg.get("src");
                    if (src != null) {
                        blockImage = fetchImageData(driver, src);
                    }
                }
            } catch (Exception e) {
                System.out.println("[验证码] 获取拼图块出错: " + e.getMessage());
            }

            // 获取元素位置
            WebElement slider = driver.findElement(By.cssSelector(".verify-move-block"));
            WebElement track = null;
            try { track = driver.findElement(By.cssSelector(".verify-bar-area, .verify-left-bar")); }
            catch (Exception e) {}

            Rectangle sliderBox = new Rectangle(
                slider.getLocation().getX(), slider.getLocation().getY(),
                slider.getSize().getWidth(), slider.getSize().getHeight()
            );
            Rectangle trackBox = track != null ? new Rectangle(
                track.getLocation().getX(), track.getLocation().getY(),
                track.getSize().getWidth(), track.getSize().getHeight()
            ) : null;
            Rectangle containerBox = new Rectangle(
                container.getLocation().getX(), container.getLocation().getY(),
                container.getSize().getWidth(), container.getSize().getHeight()
            );

            return new CaptchaPositionInfo(sliderBox, trackBox, containerBox, bgImage, blockImage, imgOffset);
        }

        private byte[] fetchImageData(WebDriver driver, String src) {
            if (src == null) return null;

            // 处理Data URL
            if (src.startsWith("data:image")) {
                try {
                    String base64 = src.substring(src.indexOf(",") + 1);
                    return Base64.getDecoder().decode(base64);
                } catch (Exception e) {
                    return null;
                }
            }

            // 使用JavaScript的fetch获取图片
            try {
                JavascriptExecutor js = (JavascriptExecutor) driver;
                String dataUrl = (String) js.executeScript(
                    "async () => {" +
                    "  const url = '" + src + "';" +
                    "  try {" +
                    "    const response = await fetch(url);" +
                    "    const blob = await response.blob();" +
                    "    return new Promise((resolve) => {" +
                    "      const reader = new FileReader();" +
                    "      reader.onloadend = () => resolve(reader.result);" +
                    "      reader.readAsDataURL(blob);" +
                    "    });" +
                    "  } catch(e) { return null; }" +
                    "}"
                );
                if (dataUrl != null && dataUrl.startsWith("data:image")) {
                    String base64 = dataUrl.substring(dataUrl.indexOf(",") + 1);
                    return Base64.getDecoder().decode(base64);
                }
            } catch (Exception e) {
                // 忽略错误
            }

            return null;
        }

        /**
         * 获取验证码各元素位置
         */
        public CaptchaPositionInfo getCaptchaPositions(WebDriver driver, WebElement sliderElement) {
            Rectangle sliderBox = new Rectangle(
                sliderElement.getLocation().getX(),
                sliderElement.getLocation().getY(),
                sliderElement.getSize().getWidth(),
                sliderElement.getSize().getHeight()
            );

            WebElement track = null;
            WebElement container = null;
            try {
                track = driver.findElement(By.cssSelector(".verify-bar-area, .verify-left-bar"));
                container = driver.findElement(By.cssSelector(".verify-img-panel, .verify-img-out, .verify-area"));
            } catch (Exception e) {
                return null;
            }

            Rectangle trackBox = new Rectangle(
                track.getLocation().getX(),
                track.getLocation().getY(),
                track.getSize().getWidth(),
                track.getSize().getHeight()
            );
            Rectangle containerBox = new Rectangle(
                container.getLocation().getX(),
                container.getLocation().getY(),
                container.getSize().getWidth(),
                container.getSize().getHeight()
            );

            CaptchaPositionInfo info = getCaptchaImages(driver, container);

            System.out.printf("[定位] 滑块位置: x=%.1f, width=%.1f%n",
                sliderBox.x, sliderBox.width);
            System.out.printf("[定位] 容器位置: x=%.1f, width=%.1f%n",
                containerBox.x, containerBox.width);
            if (info.imgOffset != null) {
                System.out.printf("[定位] 图片相对容器偏移: x=%d, y=%d%n",
                    info.imgOffset.x, info.imgOffset.y);
            }

            return new CaptchaPositionInfo(
                sliderBox, trackBox, containerBox,
                info.bgImage, info.blockImage, info.imgOffset
            );
        }

        /**
         * 模拟拖动滑块
         */
        public boolean simulateDrag(WebDriver driver, WebElement sliderElement, double distance) {
            if (distance < 50) distance = 200;

            Rectangle sliderBox = new Rectangle(
                sliderElement.getLocation().getX(),
                sliderElement.getLocation().getY(),
                sliderElement.getSize().getWidth(),
                sliderElement.getSize().getHeight()
            );

            if (sliderBox.width == 0) return false;

            double startX = sliderBox.x + sliderBox.width / 2.0;
            double startY = sliderBox.y + sliderBox.height / 2.0;

            System.out.printf("[拖动] 起点=(%.1f, %.1f), 距离=%.1fpx%n", startX, startY, distance);

            List<TrackPoint> tracks = generateDragTracks(distance);

            try {
                Actions actions = new Actions(driver);

                int offsetX = ThreadLocalRandom.current().nextInt(
                    DragConstants.RANDOM_OFFSET_MIN,
                    DragConstants.RANDOM_OFFSET_MAX + 1
                );
                int offsetY = ThreadLocalRandom.current().nextInt(
                    DragConstants.RANDOM_OFFSET_MIN,
                    DragConstants.RANDOM_OFFSET_MAX + 1
                );

                actions.moveByOffset((int) (startX + offsetX), (int) (startY + offsetY))
                       .pause(ThreadLocalRandom.current().nextInt(100, 300))
                       .clickAndHold()
                       .pause(ThreadLocalRandom.current().nextInt(80, 150));

                for (int i = 0; i < tracks.size(); i++) {
                    TrackPoint track = tracks.get(i);
                    int yJitter = ThreadLocalRandom.current().nextInt(
                        DragConstants.Y_JITTER_MIN,
                        DragConstants.Y_JITTER_MAX + 1
                    );

                    int targetX = (int) (startX + track.x);
                    int targetY = (int) (startY + yJitter);

                    actions.moveToElement(sliderElement, track.x, yJitter)
                           .pause((long) (track.delay * 1000));
                }

                actions.release()
                       .build()
                       .perform();

                System.out.printf("[拖动] 完成，共%d步%n", tracks.size());
                return true;

            } catch (Exception e) {
                System.out.println("[拖动] 出错: " + e.getMessage());
                return false;
            }
        }

        /**
         * 生成拖动轨迹 - 模拟人类真实行为
         */
        public List<TrackPoint> generateDragTracks(double distance) {
            List<TrackPoint> tracks = new ArrayList<>();

            // 随机选择人类行为模式
            String[] behaviors = {"normal", "normal", "normal", "cautious", "fast", "jitter"};
            String behavior = behaviors[ThreadLocalRandom.current().nextInt(behaviors.length)];

            int numPoints;
            double baseSpeed;
            double jitterAmount;

            switch (behavior) {
                case "cautious":
                    numPoints = ThreadLocalRandom.current().nextInt(30, 46);
                    baseSpeed = 1.3;
                    jitterAmount = 0.3;
                    break;
                case "fast":
                    numPoints = ThreadLocalRandom.current().nextInt(15, 23);
                    baseSpeed = 0.7;
                    jitterAmount = 0.8;
                    break;
                case "jitter":
                    numPoints = ThreadLocalRandom.current().nextInt(25, 41);
                    baseSpeed = 1.0;
                    jitterAmount = 1.5;
                    break;
                default: // normal
                    numPoints = ThreadLocalRandom.current().nextInt(20, 31);
                    baseSpeed = 1.0;
                    jitterAmount = 0.5;
            }

            for (int i = 0; i < numPoints; i++) {
                double progress = i / (double) (numPoints - 1);

                // easeInOutCubic缓动曲线
                double eased;
                if (progress < 0.5) {
                    eased = 4 * progress * progress * progress;
                } else {
                    eased = 1 - Math.pow(-2 * progress + 2, 3) / 2;
                }

                // 添加行为特征
                switch (behavior) {
                    case "cautious":
                        if (progress < 0.2) {
                            eased *= 0.7;
                        } else if (progress > 0.7) {
                            eased = 0.7 + (eased - 0.7) * 0.5;
                        }
                        break;
                    case "fast":
                        if (progress < 0.3) {
                            eased *= 1.2;
                        }
                        break;
                    case "jitter":
                        double jitter = ThreadLocalRandom.current().nextDouble(-jitterAmount / 100, jitterAmount / 100);
                        eased = Math.max(0, Math.min(1, eased + jitter));
                        break;
                }

                double baseX = distance * eased;

                if (ThreadLocalRandom.current().nextDouble() < 0.25) {
                    baseX += ThreadLocalRandom.current().nextDouble(-jitterAmount, jitterAmount);
                }

                int x = (int) baseX;

                // 计算延迟
                int delay;
                if (progress < 0.1) {
                    delay = ThreadLocalRandom.current().nextInt(
                        DragConstants.DELAY_START_MIN,
                        DragConstants.DELAY_START_MAX + 1
                    );
                } else if (progress < 0.25) {
                    delay = ThreadLocalRandom.current().nextInt(8, 16);
                } else if (progress < 0.7) {
                    delay = ThreadLocalRandom.current().nextInt(
                        DragConstants.DELAY_MIDDLE_MIN,
                        DragConstants.DELAY_MIDDLE_MAX + 1
                    );
                } else if (progress < 0.85) {
                    delay = ThreadLocalRandom.current().nextInt(12, 26);
                } else {
                    delay = ThreadLocalRandom.current().nextInt(
                        DragConstants.DELAY_END_MIN,
                        DragConstants.DELAY_END_MAX + 1
                    );
                }

                delay = (int) (delay * baseSpeed);
                tracks.add(new TrackPoint(x, delay / 1000.0));
            }

            // 终点精确定位
            tracks.add(new TrackPoint(
                (int) distance - ThreadLocalRandom.current().nextInt(1, 4),
                ThreadLocalRandom.current().nextInt(40, 71) / 1000.0
            ));
            tracks.add(new TrackPoint(
                (int) distance,
                ThreadLocalRandom.current().nextInt(60, 121) / 1000.0
            ));

            // 过冲回调
            if (ThreadLocalRandom.current().nextDouble() < 0.4) {
                int overshoot = ThreadLocalRandom.current().nextInt(1, 4);
                tracks.add(new TrackPoint(
                    (int) distance + overshoot,
                    ThreadLocalRandom.current().nextInt(30, 61) / 1000.0
                ));
                tracks.add(new TrackPoint(
                    (int) distance,
                    ThreadLocalRandom.current().nextInt(50, 101) / 1000.0
                ));
            }

            return tracks;
        }

        /**
         * 检查验证码是否通过
         */
        public boolean checkCaptchaPassed(WebDriver driver, WebElement sliderElement) {
            try {
                // 检查滑块是否消失
                if (!sliderElement.isDisplayed()) {
                    System.out.println("[验证] 滑块已消失，可能通过");
                    return true;
                }

                // 检查成功标志
                JavascriptExecutor js = (JavascriptExecutor) driver;
                @SuppressWarnings("unchecked")
                Map<String, Object> result = (Map<String, Object>) js.executeScript(
                    "(() => {" +
                    "  const iconSelectors = [" +
                    "    '.verify-icon-success', '.icon-success', '.success-icon'," +
                    "    '[class*=\"icon-success\"]', '[class*=\"success-icon\"]'," +
                    "    '[class*=\"success\"]', '.success', '.passed', '.pass'" +
                    "  ];" +
                    "  for (const sel of iconSelectors) {" +
                    "    const elem = document.querySelector(sel);" +
                    "    if (elem && elem.offsetParent !== null) return {type: 'icon'};" +
                    "  }" +
                    "  const textSelectors = ['.verify-success', '.success-text', '.pass-text'];" +
                    "  for (const sel of textSelectors) {" +
                    "    const elem = document.querySelector(sel);" +
                    "    if (elem && elem.offsetParent !== null) {" +
                    "      const text = (elem.textContent || '').trim();" +
                    "      if (text.includes('通过') || text.includes('成功')) return {type: 'text'};" +
                    "    }" +
                    "  }" +
                    "  const container = document.querySelector('.verify-bar-area, .verify-left-bar, [class*=\"slider\"]');" +
                    "  if (container) {" +
                    "    const className = container.className || '';" +
                    "    if (className.includes('success') || className.includes('pass')) return {type: 'container'};" +
                    "  }" +
                    "  const mask = document.querySelector('.verify-mask, .mask');" +
                    "  if (!mask || (mask.style && (mask.style.display === 'none' || mask.style.opacity === '0'))) {" +
                    "    return {type: 'mask_gone'};" +
                    "  }" +
                    "  return null;" +
                    "})()"
                );

                if (result != null) {
                    System.out.println("[验证] 检测到通过状态: " + result);
                    return true;
                }

                return false;
            } catch (Exception e) {
                System.out.println("[验证] 检测出错: " + e.getMessage());
                return false;
            }
        }

        /**
         * 检查滑块是否回到起始位置
         */
        public boolean checkSliderReturned(WebDriver driver, WebElement sliderElement) {
            try {
                Rectangle sliderBox = new Rectangle(
                    sliderElement.getLocation().getX(),
                    sliderElement.getLocation().getY(),
                    sliderElement.getSize().getWidth(),
                    sliderElement.getSize().getHeight()
                );

                WebElement track = driver.findElement(By.cssSelector(".verify-bar-area, .verify-left-bar"));
                if (track != null) {
                    Rectangle trackBox = new Rectangle(
                        track.getLocation().getX(),
                        track.getLocation().getY(),
                        track.getSize().getWidth(),
                        track.getSize().getHeight()
                    );
                    if (Math.abs(sliderBox.x - trackBox.x) < 10) {
                        return true;
                    }
                }
                return false;
            } catch (Exception e) {
                return false;
            }
        }

        /**
         * 完整的验证码求解流程
         */
        public boolean solveCaptcha(WebDriver driver, WebElement sliderElement) {
            return solveCaptcha(driver, sliderElement, null);
        }

        public boolean solveCaptcha(WebDriver driver, WebElement sliderElement, Integer maxAttempts) {
            if (sliderElement == null) {
                sliderElement = findSliderElement(driver);
                if (sliderElement == null) {
                    System.out.println("[验证码] 未找到滑块元素");
                    return false;
                }
            }

            int attempts = maxAttempts != null ? maxAttempts : captchaSolver.maxRetry;

            System.out.println("\n===============================");
            System.out.println("开始验证码求解");
            System.out.println("===============================\n");

            for (int attempt = 0; attempt < attempts; attempt++) {
                System.out.printf("=== 第 %d/%d 次尝试 ===%n", attempt + 1, attempts);

                // 获取位置信息
                WebElement container = driver.findElement(By.cssSelector(".verify-img-panel, .verify-img-out, .verify-area"));
                CaptchaPositionInfo info = getCaptchaPositions(driver, sliderElement);
                if (info == null) {
                    System.out.println("[验证码] 无法获取位置信息");
                    break;
                }

                if (info.bgImage == null || info.bgImage.length == 0) {
                    System.out.println("[验证码] 无法获取验证码图片");
                    break;
                }

                // 滑块信息
                SliderInfo sliderInfo = new SliderInfo(
                    (int) info.sliderBox.x,
                    (int) info.sliderBox.y,
                    (int) info.sliderBox.width,
                    (int) info.sliderBox.height
                );

                // 识别缺口位置
                CaptchaSolver.SlideDistanceResult result = captchaSolver.getSlideDistance(
                    info.bgImage, info.blockImage, sliderInfo
                );

                double gapXInImage = result.gapX;
                Double gapYInImage = result.gapY;
                double confidence = result.confidence;

                if (gapXInImage == 0) {
                    System.out.println("[验证码] 未找到匹配的缺口");
                    break;
                }

                // 计算距离
                double sliderLeftX = info.sliderBox.x;
                int imgOffsetX = info.imgOffset != null ? info.imgOffset.x : 0;
                double gapLeftScreenX = info.containerBox.x + imgOffsetX + gapXInImage;

                double baseDistance = gapLeftScreenX - sliderLeftX;

                System.out.printf("[验证码] 计算距离: %.1fpx%n", baseDistance);

                // 获取自适应补偿建议
                List<Integer> adjustments = captchaSolver.getAdaptiveSuggestions(baseDistance);
                System.out.println("[自适应] 调整序列: " + adjustments.subList(0, Math.min(10, adjustments.size())));

                Set<Integer> triedAdjustments = new HashSet<>();
                int maxTriesPerAttempt = 8;

                for (int adj : adjustments) {
                    if (triedAdjustments.contains(adj)) continue;
                    if (triedAdjustments.size() >= maxTriesPerAttempt) break;

                    triedAdjustments.add(adj);

                    double finalDistance = baseDistance + adj;
                    finalDistance = Math.max(30, Math.min(400, finalDistance));

                    System.out.printf("[验证码] 滑动: %.1fpx (调整: %+d)%n", finalDistance, adj);

                    if (!simulateDrag(driver, sliderElement, finalDistance)) {
                        break;
                    }

                    // 等待验证结果
                    try {
                        Thread.sleep((long) (CaptchaConstants.CAPTCHA_CHECK_WAIT * 1000));
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }

                    // 检查是否通过
                    boolean passed = checkCaptchaPassed(driver, sliderElement);
                    if (passed) {
                        System.out.println("\n===============================");
                        System.out.printf("[验证码] 通过! (调整: %+dpx)%n", adj);
                        System.out.println("===============================\n");
                        captchaSolver.recordResult(adj, true);
                        return true;
                    }

                    // 检查是否失败
                    if (checkSliderReturned(driver, sliderElement)) {
                        System.out.printf("[验证码] 滑块回到原位，调整值 %+d 无效，尝试下一个%n", adj);
                        captchaSolver.recordResult(adj, false);
                        try {
                            Thread.sleep(500);
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                        }
                        continue;
                    }

                    System.out.println("[验证码] 未通过，继续尝试...");
                    try {
                        Thread.sleep(300);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }

                // 等待验证码刷新
                System.out.println("[验证码] 等待验证码刷新...");
                try {
                    Thread.sleep(2000);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }

            System.out.println("\n[验证码] 求解结束");
            return false;
        }

        /**
         * 等待验证码出现并自动求解
         */
        public boolean waitAndSolve(WebDriver driver) {
            return waitAndSolve(driver, 300, 2);
        }

        public boolean waitAndSolve(WebDriver driver, int maxWait, int checkInterval) {
            int waited = 0;

            System.out.println("[轮询] 等待滑块验证码出现...");

            while (waited < maxWait) {
                WebElement sliderElement = findSliderElement(driver);
                if (sliderElement != null) {
                    System.out.printf("\n[检测] 发现滑块验证码！(等待 %d 秒后)%n", waited);
                    return solveCaptcha(driver, sliderElement);
                }

                try {
                    Thread.sleep(checkInterval * 1000L);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
                waited += checkInterval;

                if (waited % 10 == 0) {
                    System.out.printf("[轮询] 等待中... (%d/%d秒)%n", waited, maxWait);
                }
            }

            System.out.println("[超时] 未检测到滑块验证码");
            return false;
        }
    }

    // ==================== 便捷函数 ====================

    /**
     * 便捷函数：求解验证码
     */
    public static boolean solveCaptcha(WebDriver driver) {
        return solveCaptcha(driver, true);
    }

    public static boolean solveCaptcha(WebDriver driver, boolean debug) {
        CaptchaHandler handler = new CaptchaHandler(debug);
        return handler.solveCaptcha(driver, null);
    }

    /**
     * 便捷函数：等待并求解验证码
     */
    public static boolean waitAndSolveCaptcha(WebDriver driver) {
        return waitAndSolveCaptcha(driver, 300, true);
    }

    public static boolean waitAndSolveCaptcha(WebDriver driver, int maxWait, boolean debug) {
        CaptchaHandler handler = new CaptchaHandler(debug);
        return handler.waitAndSolve(driver, maxWait, 2);
    }

    // ==================== 主函数 ====================

    public static void main(String[] args) {
        System.out.println("PMOS 滑动验证码处理模块 (Java版本)");
        System.out.println("\n使用方法:");
        System.out.println("1. 导入 CaptchaHandler");
        System.out.println("2. 创建实例: CaptchaHandler handler = new CaptchaHandler(true);");
        System.out.println("3. 调用方法: handler.solveCaptcha(driver, null);");
        System.out.println("\n或者使用便捷函数:");
        System.out.println("  z.solveCaptcha(driver);");
        System.out.println("  z.waitAndSolveCaptcha(driver);");
        System.out.println("\n依赖项:");
        System.out.println("- OpenCV Java SDK");
        System.out.println("- Selenium WebDriver");
    }

    /**
     * 矩形类
     */
    public static class Rectangle {
        public double x;
        public double y;
        public double width;
        public double height;

        public Rectangle(double x, double y, double width, double height) {
            this.x = x;
            this.y = y;
            this.width = width;
            this.height = height;
        }
    }
}
