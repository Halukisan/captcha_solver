"""
PMOS 自动化登录模块（主入口）
功能：自动完成 账号密码登录 + 滑动验证码 + CFCA证书选择 + UKey 口令
参考：home/webcl 生产环境配置

依赖模块：
- captcha_solver: 滑动验证码识别
- cfca_handler: CFCA证书和UKey处理
"""

import hashlib
import os
import json
import time
import random
import base64
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 导入子模块
from .captcha_solver import CaptchaSolver
from .cfca_handler import CFCAHandler
from config import PMOSLoginConfig
from utils import mask_sensitive


class PMOSAutoLogin:
    """PMOS自动化登录器 - 增强版"""

    BASE_URL = "https://pmos.sc.sgcc.com.cn"

    def __init__(self, username=None, password=None, ukey_password=None,
                 headless=False, auto_close=False, config=None):
        """
        初始化登录器
        Args:
            username: 用户名/账号
            password: 密码
            ukey_password: UKey 口令
            headless: 是否无头模式
            auto_close: 是否自动关闭浏览器
            config: PMOSLoginConfig配置对象或配置文件路径
        """
        # 加载配置
        if config is None:
            self.config = PMOSLoginConfig()
        elif isinstance(config, str):
            self.config = PMOSLoginConfig(config)
        else:
            self.config = config

        # 参数优先级：直接传入 > 配置文件
        self.username = username or self.config.get('username')
        self.password = password or self.config.get('password')
        self.ukey_password = ukey_password or self.config.get('ukey_password')
        self.headless = headless if headless is not None else self.config.get('headless', False)
        self.auto_close = auto_close if auto_close is not None else self.config.get('auto_close', False)

        # 打印登录信息（隐藏敏感信息）
        print(f"[登录] 账号: {self.username}")
        pwd_mask = mask_sensitive(self.password or '', visible_chars=2)
        print(f"[登录] 密码: {pwd_mask}")
        pin_mask = mask_sensitive(self.ukey_password or '', visible_chars=2) if self.ukey_password else '未设置'
        print(f"[登录] UKey PIN: {pin_mask}")

        # 获取验证码配置
        captcha_config = self.config.get_captcha_config()
        max_retry = captcha_config.get('max_retry', 6)

        self.playwright = None
        self.context = None
        self.page = None
        self.auth_info = {}
        self.user_data_dir = None

        self.captcha_solver = CaptchaSolver(
            debug=self.config.get('debug', True),
            max_retry=max_retry
        )
        self.login_type = self.config.get('login_type', 'CF_CA')

    def login(self):
        """执行完整的登录流程"""
        self.playwright = sync_playwright().start()

        print("启动浏览器...")

        # 1. 优先使用配置文件中的浏览器路径
        chrome_path = self.config.get('browser_path')
        if chrome_path and os.path.exists(chrome_path):
            print(f"使用配置的浏览器: {chrome_path}")
        else:
            # 2. 查找系统浏览器路径（Chrome 和 Edge）
            browser_paths = [
                # Chrome
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(os.getenv('USERNAME', '')),
                # Edge
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]

            chrome_path = None
            for path in browser_paths:
                if os.path.exists(path):
                    chrome_path = path
                    print(f"找到系统浏览器: {chrome_path}")
                    break

            if not chrome_path:
                print("未找到本地浏览器，使用 Playwright 自带的 Chromium")

        # 使用临时目录（因为固定目录在当前环境下有问题）
        # 每次运行会创建新的临时目录，但能正常工作
        import tempfile
        import string
        import random
        random_suffix = ''.join(random.choices(string.ascii_lowercase, k=8))
        self.user_data_dir = os.path.join(tempfile.gettempdir(), f"chrome_pmos_{random_suffix}")
        os.makedirs(self.user_data_dir, exist_ok=True)

        print(f"使用用户数据目录: {self.user_data_dir}")

        # 使用系统 Chrome，移除 viewport 让页面自适应
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            channel="chrome",
            headless=self.headless,
            # 不设置 viewport，让浏览器使用实际窗口大小
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--start-maximized',  # 最大化窗口
            ],
        )

        # 反检测脚本 - 增强版
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            delete navigator.__proto__.webdriver;

            // 覆盖 plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            // 覆盖 languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en-US', 'en']
            });
        """)

        self.page = self.context.new_page()

        # 资源拦截 - 加速页面加载，拦截非必要资源
        def block_resources(route):
            resource_type = route.request.resource_type
            if resource_type in ['image', 'font', 'media']:
                route.abort()
            else:
                route.continue_()

        self.page.route("**/*", block_resources)

        # 收集网络请求
        self._setup_request_logging()

        # 访问登录页
        print(f"访问 {self.BASE_URL}")
        self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)

        # 登录流程
        print("\n" + "="*50)
        print("开始登录流程")
        print("="*50 + "\n")

        # 步骤1: 输入账号密码
        print("[1/4] 输入账号密码")
        if not self._enter_credentials():
            return None

        # 步骤2: 点击登录
        print("\n[2/4] 点击登录按钮")
        if not self._click_login_button():
            return None

        # 步骤3: 处理验证码
        print("\n[3/4] 处理滑动验证码")
        # 尝试等待用户手动处理
        # input("请手动完成滑动验证码，完成后按回车键继续...")
        try:
            self._handle_slider_captcha()
        except Exception as e:
            print(f"验证码处理异常: {e}")

        # 步骤4: CFCA证书和UKey
        print("\n[4/4] 处理CFCA证书和UKey")
        try:
            cfca_handler = CFCAHandler(self.page, debug=self.config.get('debug', True))
            cfca_handler.handle_cfca_popup(self.ukey_password)
        except Exception as e:
            print(f"CFCA处理异常: {e}")

        # 等待登录完成
        print("\n等待登录完成...")
        try:
            self.page.wait_for_url("**/outNet", timeout=30000)
            print("登录成功！")
        except PlaywrightTimeoutError:
            current_url = self.page.url
            print(f"当前页面: {current_url}")
            if "outNet" in current_url:
                print("登录成功！")
            else:
                print("登录状态不确定，请确认...")
                time.sleep(3)

        # 等待并捕获 X-Ticket（可能需要额外请求触发）
        print("\n等待 X-Ticket 生成...")
        time.sleep(3)  # 等待页面完全加载

        # 轮询等待 X-Ticket cookie 出现（最多等待15秒）
        print("轮询等待 X-Ticket cookie...")
        xticket_found = False
        for i in range(20):
            time.sleep(1)
            current_cookies = self.context.cookies()
            cookie_names = [c["name"] for c in current_cookies]
            print(f"  第{i+1}秒: cookies = {cookie_names}")

            # 检查是否找到 X-Ticket（必需的）
            if any('x-ticket' in name.lower() or 'X-Ticket' in name for name in cookie_names):
                print("  ✓ 找到 X-Ticket!")
                xticket_found = True
                print("X-Ticket 已生成，继续...")
                break

            # 检查其他认证 cookie（可选，用于调试）
            for needed in ['Admin-Token']:
                if any(needed in name for name in cookie_names):
                    print(f"  ✓ 找到 {needed}")
        else:
            print("  警告: 20秒后仍未找到 X-Ticket cookie")

        # 尝试触发一个 API 请求来获取 X-Ticket
        print("尝试触发 API 请求获取认证信息...")
        try:
            # 访问一个需要认证的页面来触发 X-Ticket 设置
            self.page.goto(f"{self.BASE_URL}/px-sbs-out/pxSbs/doFunc", wait_until="domcontentloaded", timeout=10000)
            time.sleep(2)
        except Exception as e:
            print(f"API 请求触发: {e}")

        # 再次等待并重新提取 cookies
        time.sleep(2)

        # 提取认证信息
        self.auth_info = self._extract_auth()
        if self.auth_info:
            self._save_auth()

        # 关闭处理
        if self.auto_close:
            print("\n浏览器将自动关闭...")
            time.sleep(1)
            self.close()
        else:
            print("\n保持浏览器在后台运行，认证信息已保存...")
            print("API将使用保存的认证信息进行调用")

        return self.auth_info

    def close(self):
        """关闭浏览器，保留缓存目录以加速下次启动"""
        if self.context:
            self.context.close()
            self.context = None
        # 不再删除用户数据目录，保留缓存
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
        print("[浏览器] 已关闭 (缓存已保留)")

    def _setup_request_logging(self):
        """设置网络请求日志"""
        self.requests_log = []
        self.responses_log = []
        self.api_responses = []
        self.captured_ticket = None  # 存储捕获的 X-Ticket

        def log_request(request):
            if any(k in request.url.lower() for k in ['ticket', 'token', 'auth']):
                self.requests_log.append({'url': request.url, 'method': request.method})
                # 打印请求 headers 中的 ticket
                headers = request.headers
                if 'X-Ticket' in headers:
                    print(f"[网络] 请求 X-Ticket: {headers['X-Ticket'][:30]}...")
                    self.captured_ticket = headers['X-Ticket']

        def log_response(response):
            if any(k in response.url.lower() for k in ['ticket', 'auth', 'login']):
                self.responses_log.append({'url': response.url, 'status': response.status})
                # 打印响应 headers 中的 ticket
                headers = response.headers
                if 'X-Ticket' in headers:
                    print(f"[网络] 响应 X-Ticket: {headers['X-Ticket'][:30]}...")
                    self.captured_ticket = headers['X-Ticket']
                # 打印 set-cookie 中的 ticket
                if 'set-cookie' in headers:
                    cookies = headers['set-cookie']
                    if 'X-Ticket' in cookies:
                        print(f"[网络] Set-Cookie X-Ticket found!")

        def capture_api_response(response):
            if 'application/json' in response.headers.get('content-type', ''):
                try:
                    body = response.json()
                    self.api_responses.append({'url': response.url, 'body': body})
                    # 检查响应体中的 ticket
                    if isinstance(body, dict) and 'data' in body:
                        data = body.get('data', {})
                        if isinstance(data, dict) and 'ticket' in data:
                            print(f"[网络] 响应体 ticket: {data['ticket'][:30]}...")
                            self.captured_ticket = data['ticket']
                except:
                    pass

        self.page.on("request", log_request)
        self.page.on("response", log_response)
        self.page.on("response", capture_api_response)

    def _enter_credentials(self):
        """输入账号密码"""
        print("等待账号密码输入框...")
        time.sleep(1)

        # 使用 JavaScript 快速填充
        result = self.page.evaluate("""
            () => {
                const usernameSelectors = ["input[name='username']", "input[name='user']",
                    "input[placeholder*='账号']", "input[placeholder*='用户名']", "#username", "#user"];
                const passwordSelectors = ["input[name='password']", "input[placeholder*='密码']", "#password"];
                let usernameInput = null, passwordInput = null;
                for (let sel of usernameSelectors) {
                    const elem = document.querySelector(sel);
                    if (elem && elem.offsetParent !== null) { usernameInput = elem; break; }
                }
                for (let sel of passwordSelectors) {
                    const elem = document.querySelector(sel);
                    if (elem && elem.offsetParent !== null) { passwordInput = elem; break; }
                }
                return {usernameFound: usernameInput !== null, passwordFound: passwordInput !== null};
            }
        """)

        print(f"[输入] 账号框: {'找到' if result.get('usernameFound') else '未找到'}")
        print(f"[输入] 密码框: {'找到' if result.get('passwordFound') else '未找到'}")

        if not result.get('usernameFound') or not result.get('passwordFound'):
            # 传统方法
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            username_input = None
            password_input = None
            for sel in ["input[name='username']", "#username", "input[type='text']"]:
                try:
                    username_input = self.page.wait_for_selector(sel, timeout=3000)
                    if username_input: break
                except: continue
            for sel in ["input[name='password']", "#password", "input[type='password']"]:
                try:
                    password_input = self.page.wait_for_selector(sel, timeout=3000)
                    if password_input: break
                except: continue

            if username_input and password_input:
                if self.username: username_input.fill(self.username)
                if self.password: password_input.fill(self.password)
                return True
            return False

        # JavaScript 填充
        if self.username:
            self.page.evaluate(f"""
                () => {{
                    const selectors = ["input[name='username']", "input[name='user']",
                        "input[placeholder*='账号']", "#username", "input[type='text']"];
                    for (let sel of selectors) {{
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {{
                            elem.value = '{self.username}';
                            elem.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            break;
                        }}
                    }}
                }}
            """)
            print(f"已输入账号: {self.username}")

        if self.password:
            safe_pwd = self.password.replace("\\", "\\\\").replace("'", "\\'")
            self.page.evaluate(f"""
                () => {{
                    const selectors = ["input[name='password']", "input[placeholder*='密码']", "#password"];
                    for (let sel of selectors) {{
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {{
                            elem.value = '{safe_pwd}';
                            elem.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            break;
                        }}
                    }}
                }}
            """)
            print("已输入密码")

        time.sleep(0.2)
        return True

    def _click_login_button(self):
        """点击登录按钮（增强版）"""
        print("查找登录按钮...")

        # 先打印页面上的按钮信息
        button_info = self.page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button, input[type="submit"], input[type="button"]');
                const info = [];
                for (const btn of buttons) {
                    if (btn.offsetParent !== null) {
                        const text = (btn.value || btn.textContent || btn.title || '').trim();
                        const type = btn.type || btn.tagName || '';
                        const className = btn.className || '';
                        info.push({text: text.substring(0, 30), type: type, className: className.substring(0, 50)});
                    }
                }
                return info;
            }
        """)
        print(f"[登录] 页面上的按钮: {button_info}")

        login_selectors = [
            "button[type='submit']",
            "button:has-text('登录')",
            "button:has-text('登 录')",
            "input[type='submit']",
            ".login-btn",
            ".btn-login",
            "#login-btn",
            "button.el-button--primary",
            "button.ant-btn-primary",
        ]

        for selector in login_selectors:
            try:
                btn = self.page.wait_for_selector(selector, timeout=3000)
                if btn and btn.is_visible():
                    print(f"[登录] 找到按钮: {selector}")
                    btn.click()
                    time.sleep(0.5)
                    return True
            except:
                continue

        # JavaScript 点击 - 更强的匹配
        result = self.page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button, input[type="submit"], input[type="button"]');
                for (const btn of buttons) {
                    if (btn.offsetParent === null) continue;
                    const text = (btn.value || btn.textContent || '').trim();
                    if (text.includes('登录') || text === '登 录') {
                        btn.click();
                        return {clicked: true, text: text};
                    }
                }
                return {clicked: false};
            }
        """)

        if result.get('clicked'):
            print(f"[登录] JavaScript点击成功: {result.get('text')}")
            return True

        print("[登录] 未找到登录按钮，尝试回车键提交")
        # 尝试回车提交
        self.page.keyboard.press("Enter")
        time.sleep(0.5)
        return True

    def _handle_slider_captcha(self):
        """
        处理滑动验证码（检测刷新版）
        核心流程：检测滑块元素 -> 获取验证码图片 -> 识别缺口位置 -> 模拟人类拖动
        关键技术点：
        1. 检测验证码刷新机制：通过图片哈希对比判断验证码是否已刷新
        2. 支持slide2双图模式：分别获取背景图和拼图块图
        3. 智能重试策略：最多尝试6次，每次失败后等待刷新并调整滑动距离
        4. 拟人化拖动：使用贝塞尔曲线模拟人类滑动轨迹，加入随机抖动和延迟
        """
        print("检查滑动验证码...")
        time.sleep(5)  # 等待验证码完全加载，确保DOM渲染完成

        # 调试：打印验证码区域结构
        self._debug_captcha_structure()

        # 查找滑块
        slider_element = self._find_slider_element()
        if not slider_element:
            print("未检测到滑动验证码")
            return True

        print("检测到滑动验证码")

        # 保存上一次验证码图片用于对比刷新
        last_bg_image = None
        last_bg_hash = None

        # 最多尝试次数（参考生产配置为6次）
        captcha_config = self.config.get_captcha_config()
        max_attempts = captcha_config.get('max_retry', 6)

        # 主循环：尝试最多max_attempts次滑动验证
        # 每次循环包含：检测刷新、获取元素位置、识别缺口、模拟拖动、验证结果
        for attempt in range(max_attempts):
            print(f"\n=== 第 {attempt + 1}/{max_attempts} 次尝试 ===")

            # 如果不是第一次，等待验证码刷新并检测
            # 验证码失败后通常会刷新图片，防止重复使用同一张图攻击
            # 检测机制：通过对比前后两张验证码图片的MD5哈希值判断是否刷新
            if attempt > 0 and last_bg_image is not None:
                print("[验证码] 等待验证码刷新...")
                # 最多等待9秒检测刷新（10次循环，每次5秒）
                refreshed = False
                for wait_i in range(10):
                    time.sleep(5)  # 每次等待5秒，避免频繁查询
                    current_container = self.page.query_selector(".verify-img-panel, .verify-img-out, .verify-area")
                    if current_container:
                        current_image = current_container.screenshot()
                        current_hash = hashlib.md5(current_image).hexdigest()
                        if current_hash != last_bg_hash:
                            print(f"[验证码] 检测到图片刷新 (等待{5 * (wait_i + 1)}秒后)")
                            last_bg_image = current_image
                            last_bg_hash = current_hash
                            refreshed = True
                            break
                if not refreshed:
                    print("[验证码] 未检测到刷新，使用新截图继续")
                    # 即使没检测到刷新，也重新获取截图，避免使用旧图
                    last_bg_image = None

            # 每次重新获取元素位置（验证码可能换了）
            slider_element = self._find_slider_element()
            if not slider_element:
                print("[验证码] 滑块元素消失，可能已通过")
                return True

            # 获取当前位置信息 - 每次都重新截图，确保获取最新的元素坐标
            # 返回包含滑块位置、轨道位置、容器位置、背景图、拼图块的元组
            captcha_info = self._get_captcha_positions(slider_element)
            if not captcha_info:
                print("[验证码] 无法获取位置信息")
                break

            slider_box, track_box, container_box, bg_image, block_image = captcha_info

            # 保存当前截图用于下次对比
            if bg_image:
                import hashlib
                last_bg_image = bg_image
                # 该字段没有用上
                last_bg_hash = hashlib.md5(bg_image).hexdigest()

            print(f"[验证码] 滑块中心: ({slider_box['x'] + slider_box['width']/2:.1f}, {slider_box['y'] + slider_box['height']/2:.1f})")
            print(f"[验证码] 容器尺寸: {container_box['width']:.1f}x{container_box['height']:.1f}")

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

            # 使用边缘检测法识别缺口（传入滑块图片用于精准Y坐标提取）
            # 核心算法：二维边缘轮廓形状匹配，专杀同水平线上1真多假缺口
            # 输入：背景图、拼图块图、滑块信息；输出：缺口X坐标、Y坐标、置信度
            result = self.captcha_solver.get_slide_distance(bg_image, block_image, slider_info)

            # 解析结果：支持新格式 (gap_x, gap_y, confidence) 和旧格式 (gap_x,)
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

            # 计算滑块和缺口的y坐标，检查是否在同一水平线
            slider_y = slider_box['y']
            slider_h = slider_box['height']
            container_y = container_box['y']
            container_h = container_box['height']

            # 缺口在容器中的y坐标（相对于容器）
            gap_y_relative = gap_y_in_image if gap_y_in_image is not None else container_h / 2
            # 缺口在页面上的绝对y坐标
            gap_y_screen = container_y + gap_y_relative

            # 检查滑块和缺口是否在同一水平线上（容差±20像素）
            y_diff = abs(slider_y - gap_y_screen)
            same_horizontal_line = y_diff < 20

            print(f"[验证码] 滑块y={slider_y:.1f}, 缺口y={gap_y_screen:.1f}, y差值={y_diff:.1f}px")
            print(f"[验证码] 同一水平线: {'是' if same_horizontal_line else '否'}, 置信度={confidence:.2f}")

            # 计算距离：滑块左边缘 到 缺口左边缘 的距离
            slider_left_x = slider_box['x']
            gap_left_screen_x = container_box['x'] + gap_x_in_image
            distance = gap_left_screen_x - slider_left_x

            print(f"[验证码] 滑块左x={slider_left_x:.1f}, 缺口左x={gap_left_screen_x:.1f}, 距离={distance:.1f}px")

            # 如果不在同一水平线，给出警告
            if not same_horizontal_line:
                print(f"[验证码] 警告: 滑块和缺口可能不在同一水平线上！")
                print(f"[验证码] 滑块y范围: {slider_y:.1f}-{slider_y + slider_h:.1f}")
                print(f"[验证码] 缺口y范围: {gap_y_screen:.1f}-{gap_y_screen + 20:.1f} (估算)")

            # 尝试不同的调整值 - 使用更细粒度的微调策略
            # 改进版：先尝试小范围微调（±5像素），再扩大范围
            # 为什么需要微调：由于图像识别误差、浏览器渲染差异、鼠标拖动精度等因素，
            # 计算出的理论距离可能不完全准确，需要通过微调补偿这些误差
            adjustments = captcha_config.get('adjustments', [
                0,  # 不调整，测试原始计算
                # ±1-2像素的精细调整（用于微小误差）
                -1, 1, -2, 2,
                # ±3-5像素（常见误差范围）
                -3, 3, -4, 4, -5, 5,
                # ±6-8像素（较大误差）
                -6, 6, -7, 7, -8, 8,
                # ±10-15像素（较大偏移，用于特殊情况的补偿）
                -10, 10, -12, 12, -15, 15
            ])

            for adj in adjustments:
                final_distance = distance + adj
                final_distance = max(30, min(400, final_distance))

                print(f"[验证码] 滑动: {final_distance:.1f}px (调整: {adj:+d})")

                if not self._simulate_human_drag(slider_element, final_distance):
                    break

                # 等待验证结果
                time.sleep(3)

                # 检查是否通过
                passed = self._check_captcha_passed(slider_element)
                if passed:
                    print("[验证码] 通过！")
                    return True

                # 检查是否失败了（滑块回到原位）
                if self._check_slider_returned(slider_element):
                    print("[验证码] 滑块回到原位，验证失败，等待刷新...")
                    break

                print("[验证码] 未通过，继续尝试...")

                # 每次失败后等待更长时间，避免触发反爬
                if adj != adjustments[0]:  # 不是第一次尝试
                    time.sleep(3)

        print("验证码处理完成")
        return True

    def _find_slider_element(self):
        """查找滑块元素"""
        captcha_configs = [
            {"slider": ".verify-move-block", "track": ".verify-left-bar", "check": ".verify-bar-area"},
            {"slider": "#nc_1__scale_text", "track": ".nc_1__scale_text", "check": ".yidun_intelligence"},
            {"slider": "[class*='slider-btn']", "track": "[class*='slider-track']", "check": "[class*='captcha']"},
        ]

        for config in captcha_configs:
            try:
                if self.page.query_selector(config["check"]):
                    slider = self.page.wait_for_selector(config["slider"], timeout=2000)
                    if slider:
                        return slider
            except: continue

        # 模糊匹配
        fuzzy_selectors = ["[class*='slider-btn']", "[class*='slide-btn']", ".yidun_slider__icon"]
        for selector in fuzzy_selectors:
            try:
                elements = self.page.query_selector_all(selector)
                for elem in elements:
                    if elem.is_visible():
                        return elem
            except: continue

        return None

    def _debug_captcha_structure(self):
        """调试：打印验证码区域的结构"""
        try:
            info = self.page.evaluate("""
                () => {
                    const result = [];

                    // 查找验证码相关的容器
                    const captchaSelectors = [
                        '.verify-img-panel', '.verify-img-out', '.verify-area',
                        '[class*="captcha"]', '[class*="verify"]', '[id*="captcha"]', '[id*="verify"]'
                    ];

                    for (const sel of captchaSelectors) {
                        const elem = document.querySelector(sel);
                        if (elem) {
                            // 获取子元素信息
                            const children = [];
                            elem.querySelectorAll('*').forEach(child => {
                                const tag = child.tagName;
                                const cls = child.className || '';
                                const id = child.id || '';
                                if (tag === 'CANVAS' || tag === 'IMG' || tag === 'DIV') {
                                    children.push({
                                        tag: tag,
                                        className: cls.substring(0, 50),
                                        id: id.substring(0, 30)
                                    });
                                }
                            });

                            result.push({
                                selector: sel,
                                className: elem.className?.substring(0, 50) || '',
                                hasCanvas: elem.querySelector('canvas') !== null,
                                hasImg: elem.querySelector('img') !== null,
                                imgSrc: elem.querySelector('img')?.src?.substring(0, 100) || null,
                                children: children.slice(0, 10)
                            });
                        }
                    }

                    return result;
                }
            """)
            print(f"[调试] 验证码结构: {info}")
        except Exception as e:
            print(f"[调试] 获取结构失败: {e}")

    def _get_captcha_positions(self, slider_element):
        """获取验证码各元素位置 - 支持slide2双图模式"""
        slider_box = slider_element.bounding_box()
        track = self.page.query_selector(".verify-bar-area, .verify-left-bar")
        track_box = track.bounding_box() if track else None
        container = self.page.query_selector(".verify-img-panel, .verify-img-out, .verify-area")
        container_box = container.bounding_box() if container else None

        if not (slider_box and track_box and container_box):
            return None

        # slide2模式：获取两张图片（背景图 + 拼图块图）
        bg_image, block_image = self._get_captcha_images_slide2(container)
        if bg_image:
            print(f"[验证码] 背景图: {len(bg_image)} bytes")
        if block_image:
            print(f"[验证码] 拼图块: {len(block_image)} bytes")

        return slider_box, track_box, container_box, bg_image, block_image

    def _get_captcha_images_slide2(self, container):
        """
        slide2模式：获取两张图片
        1. 背景图（有缺口的图）
        2. 拼图块图（需要拖动的滑块图）

        关键发现：拼图块可能不是独立的图片，而是从背景图中提取出来的缺口区域
        或者拼图块在滑块元素上作为背景图
        """
        bg_image = None
        block_image = None

        # 第一步：获取背景图（大图）
        try:
            result = self.page.evaluate("""
                () => {
                    // 查找验证码区域的所有可见图片
                    const allImages = [];

                    // 各种可能的选择器
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
                                    allImages.push({
                                        src: src,
                                        width: Math.round(rect.width),
                                        height: Math.round(rect.height),
                                        area: Math.round(rect.width * rect.height),
                                        selector: sel,
                                        index: idx
                                    });
                                }
                            }
                        });
                    }

                    // 按面积排序（大到小）
                    allImages.sort((a, b) => b.area - a.area);

                    return {
                        count: allImages.length,
                        images: allImages
                    };
                }
            """)

            if result:
                images = result.get('images', [])
                print(f"[验证码] 找到 {len(images)} 张图片")

                for i, img_info in enumerate(images):
                    print(f"  [{i}] {img_info.get('width')}x{img_info.get('height')} ({img_info.get('area')}px) - {img_info.get('selector')}")

                # 获取最大的图作为背景
                if images:
                    largest = images[0]
                    src = largest.get('src')
                    if src:
                        bg_image = self._fetch_image_data(src)
                        if bg_image:
                            print(f"[验证码] 背景图: {largest.get('width')}x{largest.get('height')}")

        except Exception as e:
            print(f"[验证码] 获取背景图出错: {e}")

        # 第二步：获取拼图块
        # 拼图块可能是：
        # 1. 滑块元素的背景图
        # 2. 一个小的独立图片
        # 3. 需要从背景图提取

        block_image = None

        # 方法1：检查滑块元素的背景图
        try:
            slider_bg = self.page.evaluate("""
                () => {
                    const slider = document.querySelector('.verify-move-block');
                    if (!slider) return null;

                    // 获取背景图
                    const style = window.getComputedStyle(slider);
                    const bgImage = style.backgroundImage;

                    if (bgImage && bgImage !== 'none') {
                        // 提取url
                        const match = bgImage.match(/url\\(['"]?([^'"]+)['"]?\\)/);
                        if (match) {
                            return { src: match[1], method: 'background' };
                        }
                    }

                    // 检查是否有img子元素
                    const img = slider.querySelector('img');
                    if (img && img.src) {
                        return { src: img.src, method: 'child_img' };
                    }

                    return null;
                }
            """)

            if slider_bg:
                src = slider_bg.get('src')
                method = slider_bg.get('method', '')
                print(f"[验证码] 滑块背景图: {method}")

                # 处理base64或url
                if src and not src.startswith('data:'):
                    # 可能是相对URL
                    if not src.startswith('http'):
                        src = self.page.url.split('/').slice(0, 3).join('/') + '/' + src.lstrip('/')

                img_data = self._fetch_image_data(src)
                if img_data:
                    block_image = img_data
                    print(f"[验证码] 从滑块获取拼图块: {len(img_data)} bytes")

        except Exception as e:
            print(f"[验证码] 获取滑块背景失败: {e}")

        # 方法2：如果没有拼图块，使用传统的缺口检测方法
        # 不返回拼图块，让算法回退到单图模式
        if not block_image:
            print(f"[验证码] 未找到拼图块，将使用单图模式检测缺口")

        # 兜底：确保至少有背景图
        if not bg_image:
            try:
                bg_image = container.screenshot()
                print(f"[验证码] 容器截图作为背景图")
            except:
                pass

        return bg_image, block_image

    def _fetch_image_data(self, src):
        """获取图片数据"""
        if not src:
            return None

        # base64编码的图片
        if src.startswith('data:image'):
            try:
                import base64
                return base64.b64decode(src.split(',', 1)[1])
            except:
                pass

        # URL图片 - 通过浏览器获取
        try:
            import base64
            result = self.page.evaluate(f"""
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

    def _get_captcha_image(self, container):
        """获取验证码图片 - 多种方式"""
        # 方式1: 尝试从 canvas 获取
        canvas_img = self._get_canvas_image(container)
        if canvas_img:
            print("[验证码] 从 canvas 获取图片")
            return canvas_img

        # 方式2: 尝试从 img 标签获取
        img_data = self._get_img_src(container)
        if img_data:
            print("[验证码] 从 img src 获取图片")
            return img_data

        # 方式3: 直接截图容器
        try:
            return container.screenshot()
        except:
            pass

        return None

    def _get_canvas_image(self, container):
        """从 canvas 元素获取图片"""
        try:
            result = self.page.evaluate("""
                (container) => {
                    // 查找 canvas
                    const canvas = container.querySelector('canvas');
                    if (!canvas) return null;

                    try {
                        return canvas.toDataURL('image/png');
                    } catch (e) {
                        return null;
                    }
                }
            """, container)

            if result and result.startswith('data:image'):
                import base64
                return base64.b64decode(result.split(',', 1)[1])
        except:
            pass
        return None

    def _get_img_src(self, container):
        """从 img 标签获取图片"""
        try:
            result = self.page.evaluate("""
                (container) => {
                    const img = container.querySelector('img');
                    if (!img || !img.src) return null;

                    // 如果是 base64
                    if (img.src.startsWith('data:image')) {
                        return img.src;
                    }

                    // 如果是 URL，返回 URL
                    return img.src;
                }
            """, container)

            if result:
                import base64
                if result.startswith('data:image'):
                    return base64.b64decode(result.split(',', 1)[1])
                else:
                    # 从 URL 获取（需要处理跨域）
                    import requests
                    resp = requests.get(result, timeout=5)
                    if resp.status_code == 200:
                        return resp.content
        except:
            pass
        return None

    def _check_slider_returned(self, slider_element):
        """检查滑块是否回到起始位置（验证失败的标志）"""
        try:
            slider_box = slider_element.bounding_box()
            if not slider_box:
                return False

            # 获取轨道位置
            track = self.page.query_selector(".verify-bar-area, .verify-left-bar")
            if track:
                track_box = track.bounding_box()
                if track_box and abs(slider_box['x'] - track_box['x']) < 10:
                    return True

            return False
        except:
            return False

    def _check_captcha_passed(self, slider_element):
        """检查验证码是否通过"""
        try:
            # 检查滑块是否仍然可见
            if not slider_element.is_visible():
                print("[验证码] 滑块消失，可能通过")
                return True

            # 检查是否有成功图标或文字
            success_info = self.page.evaluate("""
                () => {
                    // 检查成功图标（通常是一个绿色对勾）
                    const iconSelectors = [
                        '.verify-icon-success', '.icon-success', '.success-icon',
                        '[class*="icon-success"]', '[class*="success-icon"]',
                        'svg[class*="success"]', 'svg[class*="check"]'
                    ];

                    for (const sel of iconSelectors) {
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {
                            return {type: 'icon', selector: sel};
                        }
                    }

                    // 检查成功文字
                    const textSelectors = [
                        '.verify-success', '.success-text', '.pass-text'
                    ];

                    for (const sel of textSelectors) {
                        const elem = document.querySelector(sel);
                        if (elem && elem.offsetParent !== null) {
                            const text = elem.textContent || '';
                            if (text.includes('通过') || text.includes('成功') || text.includes('验证通过')) {
                                return {type: 'text', selector: sel, text: text};
                            }
                        }
                    }

                    // 检查滑块容器状态
                    const container = document.querySelector('.verify-bar-area, .verify-left-bar');
                    if (container) {
                        const className = container.className || '';
                        if (className.includes('success') || className.includes('pass')) {
                            return {type: 'container', className: className};
                        }
                    }

                    return null;
                }
            """)

            if success_info:
                print(f"[验证码] 检测到成功状态: {success_info}")
                return True

            # 默认认为未通过（需要更明确的通过信号）
            return False

        except Exception as e:
            print(f"[验证码] 检查通过状态时出错: {e}")
            return False

    def _check_captcha_refreshed(self, old_container_box):
        """检查验证码是否刷新了"""
        try:
            new_container = self.page.query_selector(".verify-img-panel, .verify-img-out, .verify-area")
            if not new_container:
                return False

            new_box = new_container.bounding_box()
            if not new_box:
                return False

            # 通过图片对比判断是否刷新
            old_image = self.page.evaluate("""
                (elem) => {
                    const img = elem.querySelector('img');
                    if (img && img.src) {
                        return img.src.substring(0, 100);  // 只取前100个字符
                    }
                    return null;
                }
            """, new_container)

            new_image = self.page.evaluate("""
                (elem) => {
                    const img = elem.querySelector('img');
                    if (img && img.src) {
                        return img.src.substring(0, 100);
                    }
                    return null;
                }
            """, new_container)

            if old_image and new_image and old_image != new_image:
                print(f"[验证码] 图片已刷新")
                return True

            # 通过尺寸变化判断
            if abs(new_box['width'] - old_container_box['width']) > 5:
                print(f"[验证码] 尺寸变化，可能刷新")
                return True

        except:
            pass

        return False

    def _get_captcha_info(self, slider_element):
        """获取验证码图片和容器位置"""
        bg_image = None
        container_box = None

        try:
            captcha_containers = [".verify-img-panel", ".verify-img-out", ".verify-area"]
            for selector in captcha_containers:
                try:
                    elem = self.page.query_selector(selector)
                    if elem:
                        box = elem.bounding_box()
                        if box and box['height'] > 100:
                            container_box = box
                            break
                except: continue

            if container_box:
                panel = self.page.query_selector(".verify-img-panel, .verify-img-out, .verify-area")
                if panel:
                    bg_image = panel.screenshot()
                    print(f"[验证码] 截图获取背景图，大小: {len(bg_image)} bytes")

            if not bg_image:
                bg_selectors = [".verify-img-panel img", ".verify-img-out img"]
                for selector in bg_selectors:
                    try:
                        elem = self.page.query_selector(selector)
                        if elem:
                            src = elem.get_attribute("src")
                            if src and src.startswith("data:image"):
                                bg_image = base64.b64decode(src.split(",", 1)[1])
                                break
                    except: continue

        except Exception as e:
            print(f"[验证码] 获取图片出错: {e}")

        return bg_image, container_box

    def _simulate_human_drag(self, slider_element, distance):
        """
        模拟人类拖动滑块 - 增强版，更拟真的轨迹
        参考生产环境配置，支持更复杂的轨迹模拟
        
        核心原理：通过模拟人类鼠标操作特征来绕过验证码的行为检测
        关键技术点：
        1. 随机起始偏移：鼠标移动到滑块时加入±2像素的随机偏移，模拟人类不精确性
        2. 变速运动：使用缓动函数模拟加速->匀速->减速过程，避免匀速运动被识别为机器
        3. 随机抖动：在Y轴方向加入微小抖动，模拟手部自然颤动
        4. 动态延迟：根据拖动进度调整每一步的延迟时间，模拟人类思考反应时间
        5. 随机停顿：在按下鼠标、释放鼠标等关键节点加入随机等待时间
        
        参数：
            slider_element: 滑块元素对象
            distance: 需要拖动的像素距离
        
        返回：
            bool: 拖动是否成功执行
        """
        if not distance or distance < 50:
            distance = 200

        slider_box = slider_element.bounding_box()
        if not slider_box:
            return False

        start_x = slider_box['x'] + slider_box['width'] / 2
        start_y = slider_box['y'] + slider_box['height'] / 2

        print(f"[拖动] 起点=({start_x:.1f}, {start_y:.1f}), 距离={distance:.1f}px")

        # 使用贝塞尔曲线模拟更真实的滑动轨迹
        # 分段：加速 -> 匀速 -> 减速 -> 微调 -> 回调
        tracks = self._generate_drag_tracks(distance)

        try:
            # 移动到滑块位置（加入轻微随机偏移，模拟人类不精确性）
            offset_x = random.randint(-2, 2)
            offset_y = random.randint(-2, 2)
            self.page.mouse.move(start_x + offset_x, start_y + offset_y)
            time.sleep(random.randint(100, 300) / 1000)  # 随机等待更长

            # 按下鼠标
            self.page.mouse.down()
            time.sleep(random.randint(80, 150) / 1000)  # 按下后稍作停顿

            # 执行滑动轨迹
            for i, track in enumerate(tracks):
                # 加入Y轴微小抖动，模拟手部不稳定性
                y_jitter = random.randint(-1, 1)

                self.page.mouse.move(start_x + track['x'], start_y + y_jitter)

                # 动态延迟：根据进度调整
                progress = i / len(tracks)
                if progress < 0.3:  # 前30%：加速期，延迟较短
                    time.sleep(track['delay'] * 0.8)
                elif progress < 0.7:  # 中40%：匀速期
                    time.sleep(track['delay'])
                else:  # 后30%：减速期，延迟较长
                    time.sleep(track['delay'] * 1.2)

            # 释放前随机停顿
            time.sleep(random.randint(50, 150) / 1000)
            self.page.mouse.up()
            print(f"[拖动] 完成，共{len(tracks)}步")
            return True

        except Exception as e:
            print(f"[拖动] 出错: {e}")
            return False

    def _generate_drag_tracks(self, distance):
        """
        生成拖动轨迹 - 使用缓动函数模拟真实人类行为
        改进版：更自然的速度变化，增加加速度突变模拟
        
        核心算法：基于缓动函数（easing functions）生成拟人化鼠标移动轨迹
        支持三种轨迹模式：
        1. smooth（平滑模式）：标准的加速-匀速-减速曲线，最自然的运动轨迹
        2. accel_pause（加速停顿模式）：快速加速后短暂停顿，再继续加速，模拟犹豫行为
        3. wobble（抖动模式）：在平滑基础上加入随机微小抖动，模拟手部不稳定
        
        技术细节：
        - 使用三次贝塞尔曲线模拟变速运动
        - 随机生成轨迹点数量（20-50个点），增加不可预测性
        - 为每个轨迹点计算基于进度的延迟时间，模拟人类反应时间变化
        - 在终点附近添加微小的过冲回调（overshoot），模拟人类的修正行为
        
        参数：
            distance: 总拖动距离（像素）
        
        返回：
            list: 轨迹点列表，每个点包含x坐标（相对起点）和延迟时间（秒）
        """
        tracks = []

        # 随机选择轨迹模式
        track_pattern = random.choice(['smooth', 'accel_pause', 'wobble'])

        # 基础轨迹点数量（随机化）
        if track_pattern == 'smooth':
            num_points = random.randint(25, 35)
        elif track_pattern == 'accel_pause':
            num_points = random.randint(20, 30)
        else:  # wobble
            num_points = random.randint(35, 50)

        for i in range(num_points):
            progress = i / (num_points - 1)  # 0到1的进度

            # 根据模式选择不同的缓动函数
            if track_pattern == 'smooth':
                # 平滑加速减速
                if progress < 0.5:
                    eased = 4 * progress * progress * progress
                else:
                    eased = 1 - pow(-2 * progress + 2, 3) / 2

            elif track_pattern == 'accel_pause':
                # 加速 -> 短暂停顿 -> 再加速
                if progress < 0.3:
                    eased = progress * progress * 3  # 快速加速
                elif progress < 0.5:
                    eased = 0.27 + (progress - 0.3) * 0.1  # 停顿
                else:
                    remaining = 1 - 0.28
                    p = (progress - 0.5) / 0.5
                    eased = 0.28 + remaining * (1 - (1 - p) * (1 - p))  # 再次加速

            else:  # wobble - 模拟手抖
                if progress < 0.5:
                    eased = 4 * progress * progress * progress
                else:
                    eased = 1 - pow(-2 * progress + 2, 3) / 2
                # 添加微小抖动
                wobble = random.uniform(-0.02, 0.02)
                eased = max(0, min(1, eased + wobble))

            # 计算位置
            base_x = distance * eased

            # 添加随机位置抖动（模拟鼠标不精确）
            if random.random() < 0.3:  # 30%概率抖动
                base_x += random.uniform(-0.5, 0.5)

            x = int(base_x)

            # 计算延迟 - 模拟人类速度不均匀
            if progress < 0.15:  # 起步
                delay = random.randint(8, 20)
            elif progress < 0.7:  # 中间段
                if track_pattern == 'accel_pause' and 0.3 < progress < 0.4:
                    delay = random.randint(30, 60)  # 停顿
                else:
                    delay = random.randint(6, 15)
            else:  # 接近终点，减速
                delay = random.randint(20, 50)

            tracks.append({'x': x, 'delay': delay / 1000})

        # 确保终点精确
        # 添加接近终点的点
        tracks.append({'x': distance - 2, 'delay': random.randint(30, 50) / 1000})
        tracks.append({'x': distance, 'delay': random.randint(50, 100) / 1000})

        # 添加微小的过冲回调（模拟人类的"过头"修正行为）
        # 只在30%的情况下添加，且幅度很小
        if random.random() < 0.3:
            overshoot = random.choice([0, 1])  # 0或1像素
            if overshoot > 0:
                tracks.append({'x': distance + overshoot, 'delay': random.randint(20, 40) / 1000})
                tracks.append({'x': distance, 'delay': random.randint(30, 60) / 1000})

        return tracks

    def _extract_auth(self):
        """提取认证信息"""
        # 方法1: 使用 Playwright 的 cookies API
        cookies = self.context.cookies()
        auth_info = {cookie["name"]: cookie["value"] for cookie in cookies}

        # 调试：显示捕获的 cookies
        cookie_names = [c["name"] for c in cookies]
        print(f"[认证] Playwright 捕获的 cookies: {cookie_names}")
        print(f"[认证] Cookies 数量: {len(cookies)}")

        # 方法2: 使用 JavaScript 直接读取 document.cookie
        try:
            js_cookies = self.page.evaluate("""
                () => {
                    return document.cookie;
                }
            """)
            if js_cookies:
                print(f"[认证] JavaScript document.cookie 长度: {len(js_cookies)}")
                # 解析 cookie 字符串
                for item in js_cookies.split(';'):
                    item = item.strip()
                    if '=' in item:
                        name, value = item.split('=', 1)
                        name = name.strip()
                        value = value.strip()
                        auth_info[name] = value
                        # 检查是否是我们要找的 cookie
                        if name in ['X-Ticket', 'Admin-Token', 'Admin-Token-cookie']:
                            print(f"[认证] 从 JS 找到: {name}={value[:30]}...")
        except Exception as e:
            print(f"[认证] JS 读取 cookies 失败: {e}")

        # 方法3: 遍历所有可能的 cookie 名称
        try:
            possible_names = ['X-Ticket', 'Admin-Token', 'Admin-Token-cookie']
            found_names = list(auth_info.keys())
            print(f"[认证] 所有已捕获的 cookie 名称: {found_names}")

            for test_name in possible_names:
                if test_name in auth_info:
                    print(f"[认证] ✓ 找到 {test_name}: {auth_info[test_name][:30]}...")
        except Exception as e:
            print(f"[认证] 检查失败: {e}")

        # 如果从网络请求中捕获了 X-Ticket，添加到 auth_info
        if hasattr(self, 'captured_ticket') and self.captured_ticket:
            auth_info['X-Ticket'] = self.captured_ticket
            print(f"[认证] 从网络请求捕获 X-Ticket: {self.captured_ticket[:30]}...")

        try:
            local_storage = self.page.evaluate("() => JSON.stringify(localStorage)")
            auth_info["_localStorage"] = json.loads(local_storage)
        except: pass

        try:
            session_storage = self.page.evaluate("""
                () => {
                    const result = {};
                    for (let i = 0; i < sessionStorage.length; i++) {
                        result[sessionStorage.key(i)] = sessionStorage.getItem(sessionStorage.key(i));
                    }
                    return result;
                }
            """)
            auth_info["_sessionStorage"] = session_storage
        except: pass

        auth_info["_current_url"] = self.page.url

        # 调试：检查是否获取到 X-Ticket
        ticket = None
        if 'X-Ticket' in auth_info:
            ticket = auth_info['X-Ticket']
        elif '_localStorage' in auth_info and 'X-Ticket' in auth_info['_localStorage']:
            ticket = auth_info['_localStorage']['X-Ticket']
        elif '_sessionStorage' in auth_info and 'X-Ticket' in auth_info['_sessionStorage']:
            ticket = auth_info['_sessionStorage']['X-Ticket']

        if ticket:
            print(f"[认证] 已获取 X-Ticket (长度: {len(ticket)})")
        else:
            print(f"[认证] 警告: 未找到 X-Ticket!")

        return auth_info

    def _save_auth(self, filename="pmos_auth.json"):
        """保存认证信息"""
        save_data = {k: v for k, v in self.auth_info.items() if not k.startswith('_')}
        if "_localStorage" in self.auth_info:
            save_data["_localStorage"] = self.auth_info["_localStorage"]
        if "_sessionStorage" in self.auth_info:
            save_data["_sessionStorage"] = self.auth_info["_sessionStorage"]
        if "_current_url" in self.auth_info:
            save_data["_current_url"] = self.auth_info["_current_url"]

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        print(f"\n认证信息已保存: {filename}")
