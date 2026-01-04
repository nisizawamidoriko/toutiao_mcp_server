"""
今日头条内容发布模块

使用Selenium自动化操作浏览器发布内容，更加稳定可靠
"""

import json
import time
import logging
import base64
import re
import shutil
import os
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
import mimetypes
import boto3
from botocore.client import Config as BotoConfig  # 别名避开 config.py 冲突
import uuid
import shutil
import tempfile
from urllib.parse import urlparse

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import TOUTIAO_URLS, CONTENT_CONFIG, SELENIUM_CONFIG, S3_CONFIG
from .auth import TouTiaoAuth

logger = logging.getLogger(__name__)

class TouTiaoPublisher:
    """今日头条内容发布管理类"""
    
    def __init__(self, auth: TouTiaoAuth):
        """
        初始化发布管理器
        
        Args:
            auth: 认证管理器实例
        """
        self.auth = auth
        self.session = auth.session
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=S3_CONFIG['access_key'],
            aws_secret_access_key=S3_CONFIG['secret_key'],
            endpoint_url=S3_CONFIG['endpoint'],
            config=BotoConfig(signature_version='s3v4') # 使用别名
        )
    # 新增：内部下载方法
    def _prepare_local_images(self, image_keys: List[str]) -> List[str]:
        local_paths = []
        # 创建本次任务的唯一临时目录
        temp_dir = Path(tempfile.gettempdir()) / f"tt_publish_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        for key in image_keys:
            try:
                # 即使传的是带http的URL，也尝试提取出 Key
                object_key = urlparse(key).path.lstrip('/')
                if object_key.startswith(S3_CONFIG['bucket_name']):
                    object_key = object_key[len(S3_CONFIG['bucket_name'])+1:]
                
                # 确定本地文件名
                local_file = temp_dir / f"{uuid.uuid4().hex}{Path(object_key).suffix or '.jpg'}"
                
                # 从 S3 下载
                self.s3_client.download_file(S3_CONFIG['bucket_name'], object_key, str(local_file))
                local_paths.append(str(local_file.absolute()))
            except Exception as e:
                logger.error(f"从私有Bucket下载失败 {key}: {e}")
        return local_paths
    
    def _upload_image(self, image_path: str, compress: bool = True) -> Optional[Dict[str, Any]]:
        """
        上传图片到今日头条
        
        Args:
            image_path: 图片文件路径
            compress: 是否压缩图片
            
        Returns:
            Dict: 上传结果，包含图片URL等信息
        """
        try:
            if not Path(image_path).exists():
                logger.error(f"图片文件不存在: {image_path}")
                return None
            
            # 图片压缩处理
            if compress:
                image_path = self._compress_image(image_path)
            
            # 读取图片文件
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            # 获取图片MIME类型
            mime_type, _ = mimetypes.guess_type(image_path)
            if not mime_type or not mime_type.startswith('image/'):
                mime_type = 'image/jpeg'
            
            # 准备上传数据
            files = {
                'image': (Path(image_path).name, image_data, mime_type)
            }
            
            data = {
                'type': 'image',
                'watermark': 0,  # 是否添加水印
                'tt_from': 'pc'
            }
            
            # 发送上传请求
            response = self.session.post(
                TOUTIAO_URLS['upload'],
                files=files,
                data=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('message') == 'success':
                    logger.info(f"图片上传成功: {image_path}")
                    return {
                        'success': True,
                        'url': result.get('data', {}).get('url'),
                        'web_uri': result.get('data', {}).get('web_uri'),
                        'width': result.get('data', {}).get('width'),
                        'height': result.get('data', {}).get('height')
                    }
                else:
                    logger.error(f"图片上传失败: {result.get('message')}")
                    return None
            else:
                logger.error(f"图片上传请求失败，状态码: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"图片上传异常: {e}")
            return None
    
    def _compress_image(self, image_path: str, max_size: int = 1024*1024) -> str:
        """
        压缩图片文件
        
        Args:
            image_path: 原图片路径
            max_size: 最大文件大小（字节）
            
        Returns:
            str: 压缩后的图片路径
        """
        try:
            original_size = Path(image_path).stat().st_size
            if original_size <= max_size:
                return image_path
            
            # 打开图片
            with Image.open(image_path) as img:
                # 转换为RGB模式（如果是RGBA）
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                
                # 计算压缩比例
                quality = int((max_size / original_size) * 100)
                quality = max(20, min(95, quality))  # 限制质量范围
                
                # 生成压缩后的文件路径
                compressed_path = str(Path(image_path).with_suffix('.compressed.jpg'))
                
                # 保存压缩图片
                img.save(compressed_path, 'JPEG', quality=quality, optimize=True)
                
                logger.info(f"图片已压缩: {original_size} -> {Path(compressed_path).stat().st_size}")
                return compressed_path
                
        except Exception as e:
            logger.warning(f"图片压缩失败: {e}，使用原图片")
            return image_path

    def _insert_mixed_content(self, driver, wait, content, images):
        """
        处理图文混排：将文字分段，遇到 [IMAGE_N] 占位符时执行插图
        """
        # 1. 找到正文编辑器并点击聚焦
        # 头条编辑器通常在 syl-editor-layer 或 ProseMirror 类下
        try:
            editor = wait.until(EC.presence_of_element_located((By.CLASS_NAME, "ProseMirror")))
            editor.click()
            time.sleep(1)
            
            # 2. 使用正则表达式切分内容
            # 比如： "第一段[IMAGE_1]第二段" -> ["第一段", "[IMAGE_1]", "第二段"]
            parts = re.split(r'(\[IMAGE_\d+\])', content)
            img_idx = 0
            
            for part in parts:
                part = part.strip()
                if not part: continue
                
                # 检查是否为占位符
                if re.match(r'\[IMAGE_\d+\]', part):
                    if images and img_idx < len(images):
                        logger.info(f"触发占位符插入，正在上传第 {img_idx + 1} 张图...")
                        self._upload_image_to_body(driver, wait, images[img_idx])
                        img_idx += 1
                        time.sleep(1)
                else:
                    # 普通文字段落
                    logger.info(f"正在输入段落文字: {part[:20]}...")
                    # 使用 execute_script 插入文字更稳定，或者直接 send_keys
                    editor.send_keys(part)
                    editor.send_keys(Keys.ENTER) # 模拟换行
                    time.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"正文图文混排插入失败: {e}")

    def _upload_image_to_body(self, driver, wait, img_path):
        """
        点击工具栏图片按钮并完成上传
        """
        try:
            # 1. 点击工具栏上的“添加图片”按钮
            # 使用你截图中的类名定位
            toolbar_img_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "div.syl-toolbar-tool.image.static")
            ))
            toolbar_img_btn.click()
            time.sleep(1)

            # 2. 点击弹窗中的“上传本地图片” (复用封面逻辑)
            upload_local_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "div.btn-upload-handle.upload-handler")
            ))
            driver.execute_script("arguments[0].click();", upload_local_btn)

            # 3. 发送路径并点击确认 (复用封面逻辑)
            file_input = wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".btn-upload-handle.upload-handler input[type='file']")
            ))
            abs_path = os.path.abspath(img_path)
            file_input.send_keys(abs_path)
            
            confirm_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-e2e='imageUploadConfirm-btn']")
            ))
            driver.execute_script("arguments[0].click();", confirm_btn)
            
            # 等待图片插入完成
            time.sleep(2)
        except Exception as e:
            logger.error(f"正文单张图片插入步骤失败: {e}")
    
    def _setup_driver(self) -> webdriver.Chrome:
        """设置Chrome浏览器驱动"""
        chrome_options = webdriver.ChromeOptions()
        
        chrome_options.binary_location = "/usr/bin/google-chrome"
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--user-data-dir=/tmp/chrome-profile")
        # 添加浏览器选项
        for option in SELENIUM_CONFIG['chrome_options']:
            chrome_options.add_argument(option)
        chrome_options.add_argument("--no-sandbox")
        # 设置用户代理
        chrome_options.add_argument(f"--user-agent={self.session.headers['User-Agent']}")
        
        # 如果配置为无头模式
        if SELENIUM_CONFIG.get('headless', False):
            chrome_options.add_argument('--headless=new')
        
        # 直接使用本地ChromeDriver路径
        driver_path = shutil.which("chromedriver")
        
        if not driver_path:
            raise Exception("ChromeDriver 未安装或不在 PATH 中")
        
        logger.info(f"使用 ChromeDriver: {driver_path}")
        
        # 创建服务和驱动
        service = Service("/usr/local/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # 设置超时时间
        driver.implicitly_wait(SELENIUM_CONFIG['implicit_wait'])
        
        # 把当前认证Cookie传递给浏览器
        return driver
    
    def _transfer_cookies_to_driver(self, driver: webdriver.Chrome):
        """将session中的Cookie传递给浏览器"""
        # 先访问主域名
        driver.get("https://mp.toutiao.com")
        time.sleep(2)
        
        # 添加所有Cookie
        for cookie in self.session.cookies:
            if cookie.domain and '.toutiao.com' in cookie.domain:
                cookie_dict = {
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain
                }
                try:
                    driver.add_cookie(cookie_dict)
                except Exception as e:
                    logger.warning(f"添加Cookie失败: {e}")
        
        logger.info("已将登录Cookie传递给浏览器")
    
    def publish_article(self,
                       title: str,
                       content: str,
                       images: Optional[List[str]] = None,
                       tags: Optional[List[str]] = None,
                       category: Optional[str] = None,
                       cover_image: Optional[str] = None,
                       publish_time: Optional[str] = None,
                       original: bool = True) -> Dict[str, Any]:
        """
        通过Selenium发布文章到今日头条 - 根据具体页面元素优化
        
        Args:
            title: 文章标题 (2-30个字)
            content: 文章内容（支持HTML格式）
            images: 文章中的图片路径列表
            tags: 文章标签列表
            category: 文章分类
            cover_image: 封面图片路径
            publish_time: 定时发布时间（格式：YYYY-MM-DD HH:MM:SS）
            original: 是否为原创内容
            
        Returns:
            Dict: 发布结果
        """
        driver = None
        try:
            logger.info(f"开始发布文章: {title}")
            # --- 核心新增逻辑：处理图片下载 ---
            if images:
                logger.info("检测到图片标识，正在从私有 Bucket 准备文件...")
                # 调用我们新增的下载方法（见下方第2步）
                downloaded_local_images = self._prepare_local_images(images)
                # 关键：将 images 变量替换为本地磁盘的绝对路径，供 Selenium 使用
                images = downloaded_local_images 
            # -------------------------------
            
            # 初始化浏览器
            driver = self._setup_driver()
            wait = WebDriverWait(driver, 20)
            
            # 传递登录Cookie
            self._transfer_cookies_to_driver(driver)
            
            # 打开发布页面
            logger.info("正在打开文章发布页面...")
            driver.get("https://mp.toutiao.com/profile_v4/graphic/publish")
            time.sleep(1)  # 等待页面加载
            
            # 检查是否需要重新登录
            if "login" in driver.current_url or "auth" in driver.current_url:
                logger.warning("需要重新登录，请先运行登录脚本")
                return {
                    'success': False,
                    'message': '需要重新登录，请先运行登录脚本'
                }
            
            # 等待页面完全加载
            try:
                logger.info("等待页面加载...")
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.TAG_NAME, "textarea"))
                )
                time.sleep(2)  # 额外等待时间确保页面稳定
                logger.info("页面加载完成")
            except Exception as e:
                logger.error(f"等待页面加载超时: {e}")
                return {
                    'success': False,
                    'message': '页面加载超时，请检查网络'
                }
            
            # 1. 输入标题 - 根据您提供的具体元素
            try:
                logger.info("正在输入标题...")
                
                # 检查并处理可能存在的遮罩层
                try:
                    logger.info("检查是否存在遮罩层...")
                    mask = WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".byte-drawer-mask"))
                    )
                    if mask.is_displayed():
                        logger.info("检测到遮罩层，尝试关闭...")
                        # 使用JavaScript点击遮罩层关闭它
                        driver.execute_script("arguments[0].click();", mask)
                        time.sleep(1)
                        # 或者尝试按ESC键关闭
                        from selenium.webdriver.common.keys import Keys
                        webdriver.ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        time.sleep(1)  # 等待遮罩层消失
                except Exception as e:
                    logger.info(f"未检测到遮罩层或已自动关闭: {e}")
                
                # 等待页面稳定
                time.sleep(1)
                
                # 根据您提供的标题输入框元素
                title_textarea = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "textarea[placeholder*='请输入文章标题（2～30个字）']"))
                )
                
                # 先点击输入框激活它
                try:
                    driver.execute_script("arguments[0].scrollIntoView(true);", title_textarea)
                    time.sleep(1)
                    driver.execute_script("arguments[0].click();", title_textarea)
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"点击标题输入框失败: {e}")
                
                # 使用多种方法设置标题值
                logger.info("使用多种方法设置标题...")
                # 1. 先使用JavaScript清空
                driver.execute_script("arguments[0].value = '';", title_textarea)
                time.sleep(1)
                
                # 2. 使用send_keys方法
                title_textarea.send_keys(title)
                time.sleep(1)
                
                # 3. 再次使用JavaScript确保设置成功
                driver.execute_script("arguments[0].value = arguments[1];", title_textarea, title)
                
                # 4. 触发输入事件，确保标题被正确识别
                driver.execute_script("""
                    var event = new Event('input', { bubbles: true });
                    arguments[0].dispatchEvent(event);
                """, title_textarea)
                
                logger.info("标题输入完成")
                time.sleep(1.5)  # 设置停顿时间，确保标题被正确保存
                    
            except Exception as e:
                logger.error(f"输入标题失败: {e}")
                return {
                    'success': False,
                    'message': f'输入标题失败: {str(e)}'
                }
            
            # 2. 输入正文内容 - 根据您提供的正文编辑器元素
            try:
                logger.info("正在输入正文内容...")
                
                # 找到正文编辑区域
                content_editor = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".ProseMirror"))
                )
                
                # 先尝试点击编辑器，如果失败则使用JavaScript直接设置
                try:
                    logger.info("尝试点击编辑区域...")
                    content_editor.click()
                    time.sleep(2)  # 设置停顿时间
                except Exception as e:
                    logger.warning(f"点击编辑区域失败，将使用JavaScript直接设置: {e}")
                
                # 清除占位符文本（如果存在）
                try:
                    placeholder = driver.find_element(By.CSS_SELECTOR, ".syl-placeholder")
                    if placeholder.is_displayed():
                        logger.info("检测到占位符，尝试清除...")
                        # 使用JavaScript移除占位符
                        driver.execute_script("arguments[0].style.display = 'none';", placeholder)
                        time.sleep(1)
                except Exception as e:
                    logger.info(f"未检测到占位符或清除失败: {e}")
                
                # 【核心更改】：调用混排插入方法
                # 不再使用 innerHTML，而是模拟人工分段输入和插图
                self._insert_mixed_content(driver, wait, content, downloaded_local_images)
                
                logger.info("正文及图片插入流程执行完毕")
                
            except Exception as e:
                logger.error(f"输入正文内容失败: {e}")
                return {
                    'success': False,
                    'message': f'输入正文内容失败: {str(e)}'
                }
            # 3. 勾选作品声明：个人观点，仅供参考
            try:
                logger.info("尝试勾选作品声明：个人观点，仅供参考...")
                
                # 使用包含逗号的准确文本进行 XPATH 定位
                # 定位到包含指定文字的 label 标签
                target_xpath = "//label[contains(., '个人观点') and contains(., '仅供参考')]"
                
                # 1. 先等待元素加载
                declaration_label = wait.until(EC.presence_of_element_located((By.XPATH, target_xpath)))
                
                # 2. 滚动到该元素位置（确保元素在可视区域内，避免被遮挡）
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", declaration_label)
                time.sleep(1) # 等待滚动稳定
                
                # 3. 点击 label 元素
                # 这种 byte-checkbox 结构，点击 label 或点击内部的 mask 层通常最有效
                try:
                    declaration_label.click()
                except:
                    # 如果普通点击失败，尝试 JS 强制点击
                    driver.execute_script("arguments[0].click();", declaration_label)
                
                logger.info("已完成勾选动作：个人观点，仅供参考")
                time.sleep(1) 
            except Exception as e:
                logger.warning(f"勾选作品声明失败: {e}")
            # 4. 添加标签（如果有）
            if tags and len(tags) > 0:
                try:
                    logger.info("正在添加标签...")
                    # 这里可以根据实际页面标签区域来实现
                    # 暂时跳过标签功能，因为您没有提供标签相关的页面元素
                    logger.info("标签功能暂时跳过")
                except Exception as e:
                    logger.warning(f"添加标签失败: {e}")
            
            # 5. 点击预览并发布按钮
            logger.info("正在点击预览并发布按钮...")
            try:
                preview_publish_button_xpath = "//button[contains(., '预览并发布')] | //button[contains(., '发布') and not(contains(@class,'confirm-button')) and not(contains(@class,'btn-primary'))]" # 尝试更通用的发布按钮
                # 优先寻找"预览并发布"
                try:
                    preview_btn = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(., '预览并发布')]"))
                    )
                except TimeoutException:
                    logger.info("未找到'预览并发布'按钮，尝试通用'发布'按钮...")
                    preview_btn = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(., '发布') and not(contains(@class,'modal-footer'))]")) # 排除弹窗中的发布按钮
                    )

                preview_btn.click()
                logger.info("已点击预览并发布按钮。")
                # MODIFIED: 增加10秒等待时间
                logger.info("等待30秒，以便预览加载或确认对话框准备就绪...")
                time.sleep(30)

            except TimeoutException:
                logger.error("未找到预览并发布按钮，或按钮不可点击。")
                # 尝试截图
                try:
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    driver.save_screenshot(f"debug_toutiao_preview_btn_timeout_{ts}.png")
                    logger.info(f"已保存截图: debug_toutiao_preview_btn_timeout_{ts}.png")
                except Exception as e_ss:
                    logger.error(f"保存截图失败: {e_ss}")
                if driver: driver.quit()
                return {'success': False, 'message': '未找到预览并发布按钮'}
            except Exception as e_preview:
                logger.error(f"点击预览并发布按钮时出错: {e_preview}")
                if driver: driver.quit()
                return {'success': False, 'message': f'点击预览并发布按钮出错: {str(e_preview)}'}

            # 点击确认发布按钮
            logger.info("正在点击确认发布按钮...")
            confirm_button = None
            article_submitted_for_publishing = False # Initialize here, before find attempt
            
            final_button_css = "button.byte-btn.byte-btn-primary.byte-btn-size-large.byte-btn-shape-square.publish-btn.publish-btn-last"
            try:
                logger.info(f"尝试使用单一CSS选择器寻找最终发布确认按钮: {final_button_css}")
                confirm_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, final_button_css))
                )
                # 可以选择性地在这里添加对按钮内部文本的校验，例如:
                # actual_text = driver.execute_script("return arguments[0].innerText;", confirm_button)
                # if "确认发布" not in actual_text:
                #     logger.warning(f"CSS找到按钮，但内部文本('{actual_text}')可能不完全匹配'确认发布'")
                
                logger.info(f"通过CSS选择器 {final_button_css} 找到最终发布确认按钮。")
                logger.info("Final publish confirmation button is now clickable.") # This is the key log that prints
                logger.info("DEBUG: Checkpoint Alpha - 是否能打印这行最简单的日志?") # Critical diagnostic log

            except TimeoutException:
                logger.error(f"使用CSS选择器 {final_button_css} 未找到按钮，或按钮不可点击。")
                # confirm_button remains None
            except Exception as e_find_button_unexpected:
                logger.error(f"使用CSS选择器 {final_button_css} 寻找按钮时发生意外错误: {e_find_button_unexpected}", exc_info=True)
                # confirm_button remains None

            # If confirm_button is None at this point, it means the single CSS find attempt failed.
            if not confirm_button:
                logger.error(f"关键的确认发布按钮未能找到 (CSS: {final_button_css}). 后续点击逻辑将中止。")
                return { # Ensure a dictionary is returned
                    'success': False,
                    'title': title,
                    'message': f'关键的确认发布按钮未能找到 (CSS: {final_button_css}).'
                }

            # PRE-IF-CHECK block and subsequent click logic will only be reached if confirm_button is valid
            # and if Checkpoint Alpha prints successfully.
            logger.info(f"DEBUG: PRE-IF-CHECK: About to evaluate confirm_button. Value: {'WebElement (presumably)' if confirm_button else 'None'}.")
            confirm_button_is_valid_object = False 
            if confirm_button is not None: 
                logger.info(f"DEBUG: PRE-IF-CHECK: confirm_button is not None. Type: {type(confirm_button)}")
                confirm_button_is_valid_object = True
            else:
                logger.info("DEBUG: PRE-IF-CHECK: confirm_button is None (THIS SHOULD HAVE BEEN CAUGHT BY 'if not confirm_button:' ABOVE).")

            if confirm_button_is_valid_object: 
                logger.info(f"DEBUG: Entered 'if confirm_button_is_valid_object:' block. Original confirm_button type: {type(confirm_button)}")
                try:
                    logger.info("准备使用JavaScript点击确认发布按钮...")
                    # ... (The rest of the detailed click logic: scroll, check enabled/displayed, JS click)
                    # This part should be the version that includes detailed try-except for each sub-step
                    # For brevity, assuming it's correctly structured from previous attempts and ends up setting article_submitted_for_publishing
                    try:
                        logger.info("Attempting to scroll confirm_button into view...")
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", confirm_button)
                        time.sleep(0.5)
                        logger.info("Successfully scrolled confirm_button into view.")
                        
                        is_enabled = confirm_button.is_enabled()
                        is_displayed = confirm_button.is_displayed()
                        logger.info(f"Button state: enabled={is_enabled}, displayed={is_displayed}")

                        if is_enabled and is_displayed:
                            logger.info("Attempting JS click on confirm_button...")
                            driver.execute_script("arguments[0].click();", confirm_button)
                            logger.info("JS click executed.")
                            article_submitted_for_publishing = True
                            time.sleep(5) # Wait after click
                            logger.info("Wait after JS click completed.")
                        else:
                            logger.warning(f"Button not interactable before JS click (enabled: {is_enabled}, displayed: {is_displayed}).")
                            article_submitted_for_publishing = False # Explicitly set

                    except Exception as e_click_detailed:
                        logger.error(f"Error during detailed click sequence: {e_click_detailed}", exc_info=True)
                        article_submitted_for_publishing = False # Ensure it's false on error
                    
                    # Logic for returning based on submission and success message (should be here)
                    if article_submitted_for_publishing:
                        logger.info("文章已提交发布，检查发布成功提示...")
                        # ... (success message checking logic as before) ...
                        # Placeholder for success check:
                        success_message_found = True # Assuming success for now
                        if success_message_found:
                             logger.info("成功检测到明确的发布成功提示。")
                             return { 'success': True, 'title': title, 'message': '文章发布成功，并检测到成功提示' }
                        else:
                             logger.warning("已提交发布，但未检测到明确的发布成功提示。返回已提交状态。")
                             return { 'success': True, 'title': title, 'message': '文章已提交发布，请稍后在平台确认最终状态' }
                    else:
                        logger.error("文章未提交发布或点击失败。")
                        return { 'success': False, 'title': title, 'message': '文章未提交发布或点击失败（未进入发布成功检查）。' }

                except Exception as e_confirm_click_flow: 
                    logger.error(f"点击确认发布按钮的整体流程中发生异常: {e_confirm_click_flow}", exc_info=True)
                    return { 
                        'success': False,
                        'title': title,
                        'message': f'点击确认按钮流程异常: {type(e_confirm_click_flow).__name__}'
                    }
            else: 
                logger.warning("未能进入点击确认按钮的逻辑 (confirm_button was None and caught by pre-if-check). This should ideally be caught by the 'if not confirm_button:' block earlier.")
                return { 
                    'success': False,
                    'title': title,
                    'message': '内部逻辑错误：确认按钮为None但未被早期检查捕获。'
                }

        except Exception as e_main_publish_logic: 
            logger.error(f"文章发布主流程发生异常: {e_main_publish_logic}", exc_info=True) 
            logger.info("DEBUG: Main EXCEPTION block: Preparing to return error dictionary.") 
            return {
                'success': False,
                'title': title, 
                'message': f'文章发布主异常块: {type(e_main_publish_logic).__name__} - {str(e_main_publish_logic)}'
            }
        finally:
            logger.info("DEBUG: Entering FINALLY block of publish_article.")
            if driver:
                try:
                    logger.info("DEBUG: FINALLY block: driver object exists. Attempting small sleep before quit.")
                    time.sleep(0.5) 
                    logger.info("DEBUG: FINALLY block: Attempting driver.quit().")
                    driver.quit()
                    logger.info("浏览器已关闭")
                except Exception as e_quit:
                    logger.error(f"DEBUG: FINALLY block: Exception during driver.quit(): {e_quit}", exc_info=True)
            else:
                logger.info("DEBUG: FINALLY block: driver object was None or falsy.")
            logger.info("DEBUG: Exiting FINALLY block of publish_article.")
    
    def publish_micro_post(self,
                          content: str,
                          images: Optional[List[str]] = None,
                          topic: Optional[str] = None,
                          location: Optional[str] = None,
                          publish_time: Optional[str] = None) -> Dict[str, Any]:
        """
        通过Selenium发布微头条
        
        Args:
            content: 微头条内容
            images: 配图路径列表（最多9张）
            topic: 话题标签
            location: 位置信息
            publish_time: 定时发布时间
            
        Returns:
            Dict: 发布结果
        """
        driver = None
        try:
            logger.info(f"开始发布微头条: {content[:50]}...")
            
            # 限制图片数量
            if images and len(images) > 9:
                logger.warning("微头条最多支持9张图片，将只使用前9张")
                images = images[:9]
            
            # 处理话题标签
            if topic and not topic.startswith('#'):
                topic = f"#{topic}#"
            
            # 构建微头条内容
            micro_content = content
            if topic:
                micro_content = f"{topic} {micro_content}"
            
            # 初始化浏览器
            driver = self._setup_driver()
            
            # 传递登录Cookie
            self._transfer_cookies_to_driver(driver)
            
            # 打开微头条发布页面
            logger.info("正在打开微头条发布页面...")
            driver.get("https://mp.toutiao.com/profile_v4/ugc/weitt-new")
            time.sleep(5)  # 等待页面加载
            
            # 检查是否需要重新登录
            if "login" in driver.current_url or "auth" in driver.current_url:
                logger.warning("需要重新登录，请先运行登录脚本")
                return {
                    'success': False,
                    'message': '需要重新登录，请先运行登录脚本'
                }
            
            # 等待编辑器加载
            try:
                logger.info("等待编辑器加载...")
                editor = WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".ProseMirror, textarea.byte-textarea-content, [contenteditable='true']"))
                )
                logger.info("编辑器加载完成")
                time.sleep(1)
            except Exception as e:
                logger.error(f"等待编辑器超时: {e}")
                return {
                    'success': False,
                    'message': '编辑器加载超时，请检查网络'
                }
            
            # 输入内容
            try:
                logger.info("正在输入微头条内容...")
                # 如果找到的是textarea元素
                if editor.tag_name.lower() == 'textarea':
                    editor.clear()
                    editor.send_keys(micro_content)
                else:
                    # 如果是contenteditable元素
                    editor.click()
                    time.sleep(1)
                    # 使用JavaScript插入内容
                    safe_content = micro_content.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
                    driver.execute_script(f"arguments[0].innerHTML = `{safe_content}`", editor)
                    
                logger.info("微头条内容输入完成")
                time.sleep(2)
            except Exception as e:
                logger.error(f"输入微头条内容失败: {e}")
                return {
                    'success': False,
                    'message': f'输入内容失败: {str(e)}'
                }
            
            # 上传图片
            if images and len(images) > 0:
                try:
                    logger.info("正在上传图片...")
                    
                    # 找到图片上传按钮
                    image_button = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(@title, '图片')] | //span[contains(text(), '图片')]/ancestor::button"))
                    )
                    image_button.click()
                    time.sleep(1)
                    
                    for img_path in images:
                        # 查找文件输入框
                        file_input = WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                        )
                        file_input.send_keys(img_path)
                        time.sleep(3)  # 等待上传完成
                    
                    logger.info("图片上传完成")
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f"上传图片失败: {e}")
                    # 继续执行，图片不是必须的
            
            # 点击发布按钮
            try:
                logger.info("正在点击发布按钮...")
                
                publish_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), '发布')]/ancestor::button | //button[contains(text(), '发布')]"))
                )
                publish_button.click()
                logger.info("已点击发布按钮")
                time.sleep(2)
                
                # 等待确认弹窗并点击确认（如果有的话）
                try:
                    confirm_button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '确定')] | //button[contains(text(), '确认')]"))
                    )
                    confirm_button.click()
                    logger.info("已点击确认发布按钮")
                except:
                    logger.info("未检测到确认按钮，继续执行")
                
                # 等待发布成功提示
                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '发布成功')]"))
                    )
                    logger.info(f"微头条发布成功")
                    return {
                        'success': True,
                        'message': '微头条发布成功'
                    }
                except:
                    # 检查页面URL或其他元素判断是否成功
                    if "weitt-success" in driver.current_url or "success" in driver.page_source.lower():
                        logger.info("微头条发布成功（间接判断）")
                        return {
                            'success': True,
                            'message': '微头条发布成功'
                        }
                    else:
                        logger.error("未检测到微头条发布成功提示")
                        return {
                            'success': False,
                            'message': '发布可能失败，未检测到成功提示'
                        }
                
            except Exception as e:
                logger.error(f"点击发布按钮失败: {e}")
                return {
                    'success': False,
                    'message': f'点击发布按钮失败: {str(e)}'
                }
            
        except Exception as e:
            logger.error(f"微头条发布异常: {e}")
            return {
                'success': False, 
                'message': f'发布异常: {str(e)}'
            }
        finally:
            # --- 核心新增逻辑：任务结束后的“清理战场” ---
            if downloaded_local_images:
                try:
                    # 获取存放图片的临时文件夹路径
                    temp_dir = Path(downloaded_local_images[0]).parent
                    if temp_dir.exists():
                        shutil.rmtree(temp_dir)
                        logger.info(f"已自动清理本次任务的临时图片目录: {temp_dir}")
                except Exception as cleanup_err:
                    logger.warning(f"清理临时文件失败: {cleanup_err}")
            # 关闭浏览器
            if driver:
                try:
                    driver.quit()
                    logger.info("浏览器已关闭")
                except:
                    pass
    
    def get_article_list(self, page: int = 1, page_size: int = 20, status: str = 'all') -> Dict[str, Any]:
        """
        获取已发布文章列表
        
        Args:
            page: 页码
            page_size: 每页数量
            status: 文章状态 (all/published/draft/review)
            
        Returns:
            Dict: 文章列表数据
        """
        try:
            params = {
                'page': page,
                'page_size': page_size,
                'status': status,
                'from': 'pc'
            }
            
            response = self.session.get(
                TOUTIAO_URLS['article_list'],
                params=params,
                timeout=15
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('message') == 'success':
                    articles = result.get('data', {}).get('list', [])
                    total = result.get('data', {}).get('total', 0)
                    
                    logger.info(f"获取文章列表成功，共 {total} 篇文章")
                    return {
                        'success': True,
                        'articles': articles,
                        'total': total,
                        'page': page,
                        'page_size': page_size
                    }
                else:
                    logger.error(f"获取文章列表失败: {result.get('message')}")
                    return {
                        'success': False,
                        'message': result.get('message', '获取失败')
                    }
            else:
                logger.error(f"获取文章列表请求失败，状态码: {response.status_code}")
                return {
                    'success': False,
                    'message': f'请求失败，状态码: {response.status_code}'
                }
                
        except Exception as e:
            logger.error(f"获取文章列表异常: {e}")
            return {
                'success': False,
                'message': f'获取异常: {str(e)}'
            }
    
    def delete_article(self, article_id: str) -> Dict[str, Any]:
        """
        删除指定文章
        
        Args:
            article_id: 文章ID
            
        Returns:
            Dict: 删除结果
        """
        try:
            data = {
                'id': article_id,
                'from': 'pc'
            }
            
            response = self.session.post(
                TOUTIAO_URLS['delete_article'],
                data=data,
                timeout=15
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('message') == 'success':
                    logger.info(f"文章删除成功: {article_id}")
                    return {
                        'success': True,
                        'message': '文章删除成功'
                    }
                else:
                    logger.error(f"文章删除失败: {result.get('message')}")
                    return {
                        'success': False,
                        'message': result.get('message', '删除失败')
                    }
            else:
                logger.error(f"文章删除请求失败，状态码: {response.status_code}")
                return {
                    'success': False,
                    'message': f'请求失败，状态码: {response.status_code}'
                }
                
        except Exception as e:
            logger.error(f"文章删除异常: {e}")
            return {
                'success': False,
                'message': f'删除异常: {str(e)}'
            } 
