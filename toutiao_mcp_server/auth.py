"""
今日头条认证模块
"""

import json
import os
import time
import logging
import shutil
from typing import Dict, Optional, Any
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from .config import (
    TOUTIAO_URLS, 
    DEFAULT_HEADERS, 
    SELENIUM_CONFIG,
    get_cookies_file_path
)

logger = logging.getLogger(__name__)

class TouTiaoAuth:
    """今日头条认证管理类"""
    
    def __init__(self, cookies_file: Optional[str] = None):
        """
        初始化认证管理器
        """
        self.cookies_file = cookies_file or get_cookies_file_path()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._load_cookies()
    
    def _load_cookies(self) -> None:
        """从文件加载 Cookie"""
        try:
            if Path(self.cookies_file).exists():
                with open(self.cookies_file, 'r', encoding='utf-8') as f:
                    cookies_data = json.load(f)
                    
                # 将 Cookie 添加到 session
                for cookie in cookies_data.get('cookies', []):
                    self.session.cookies.set(
                        cookie['name'], 
                        cookie['value'],
                        domain=cookie.get('domain', '.toutiao.com')
                    )
                    
                logger.info(f"已加载 {len(cookies_data.get('cookies', []))} 个 Cookie")
        except Exception as e:
            logger.warning(f"加载 Cookie 失败: {e}")
    
    def _save_cookies(self, cookies: list) -> None:
        """保存 Cookie 到文件"""
        try:
            # 确保目录存在
            Path(self.cookies_file).parent.mkdir(parents=True, exist_ok=True)
            
            cookies_data = {
                'cookies': cookies,
                'timestamp': int(time.time())
            }
            
            with open(self.cookies_file, 'w', encoding='utf-8') as f:
                json.dump(cookies_data, f, ensure_ascii=False, indent=2)
                
            logger.info(f"已保存 {len(cookies)} 个 Cookie")
        except Exception as e:
            logger.error(f"保存 Cookie 失败: {e}")
    
    def _setup_driver(self) -> webdriver.Chrome:
        """设置 Chrome 浏览器驱动"""
        chrome_options = Options()
        
        chrome_options.binary_location = "/usr/bin/google-chrome"
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--user-data-dir=/tmp/chrome-profile")
        # 添加浏览器选项
        for option in SELENIUM_CONFIG['chrome_options']:
            chrome_options.add_argument(option)
        
        # 设置用户代理
        chrome_options.add_argument(f"--user-agent={DEFAULT_HEADERS['User-Agent']}")
        
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
        
        return driver
    
    def login_with_selenium(self, username: Optional[str] = None, password: Optional[str] = None) -> bool:
        """
        使用 Selenium 自动登录
        
        Args:
            username: 用户名（手机号/邮箱）
            password: 密码
            
        Returns:
            bool: 登录是否成功
        """
        driver = None
        try:
            driver = self._setup_driver()
            logger.info("正在打开今日头条登录页面...")
            
            # 访问登录页面
            driver.get(TOUTIAO_URLS['login'])
            
            # 等待页面加载完成
            wait = WebDriverWait(driver, SELENIUM_CONFIG['explicit_wait'])
            
            # 等待页面加载完成，不进行自动填写
            logger.info("页面加载完成，请手动进行登录操作")
            time.sleep(3)  # 给页面一些时间完全加载
            # 等待用户手动完成登录
            logger.info("请在浏览器中完成登录...")
            logger.info("注意事项：")
            logger.info("1. 今日头条通常使用手机号+验证码登录")
            logger.info("2. 请手动输入手机号")
            logger.info("3. 点击获取验证码")
            logger.info("4. 输入收到的验证码")
            logger.info("5. 点击登录按钮")
            logger.info("6. 等待登录成功跳转")
            
            # 检查是否登录成功（检查是否跳转到主页或出现登录后的特征元素）
            success = False
            wait_time = 300  # 增加到5分钟等待时间
            
            logger.info(f"等待登录完成，最多等待{wait_time}秒...")
            
            for i in range(wait_time):
                current_url = driver.current_url
                logger.debug(f"当前URL: {current_url}")
                
                # 检查是否跳转到创作者中心
                if ('mp.toutiao.com/profile' in current_url or 
                    'creator.toutiao.com' in current_url or
                    'mp.toutiao.com/dashboard' in current_url):
                    success = True
                    logger.info(f"检测到登录成功，跳转到: {current_url}")
                    break
                
                # 每30秒提示一次进度
                if i % 30 == 0 and i > 0:
                    logger.info(f"等待中... 已等待{i}秒，剩余{wait_time-i}秒")
                
                time.sleep(1)
            
            if success:
                # 获取所有 Cookie
                cookies = driver.get_cookies()
                self._save_cookies(cookies)
                
                # 更新 session 的 Cookie
                for cookie in cookies:
                    self.session.cookies.set(
                        cookie['name'], 
                        cookie['value'],
                        domain=cookie.get('domain', '.toutiao.com')
                    )
                
                logger.info("登录成功，已保存 Cookie")
                return True
            else:
                logger.error("登录超时或失败")
                return False
                
        except Exception as e:
            logger.error(f"登录过程出错: {e}")
            return False
        finally:
            if driver:
                driver.quit()
    
    def check_login_status(self) -> bool:
        """
        检查当前登录状态
        
        Returns:
            bool: 是否已登录
        """
        try:
            # 尝试访问创作者中心首页
            homepage_url = TOUTIAO_URLS['homepage']
            response = self.session.get(homepage_url, timeout=10)
            
            # 检查响应状态
            if response.status_code == 200:
                # 检查是否包含登录后才有的内容
                response_text = response.text.lower()
                
                # 检查多个可能的登录标识
                login_indicators = [
                    'profile',
                    'creator',
                    'dashboard', 
                    'publish',
                    'content',
                    '创作者',
                    '发布',
                    '我的'
                ]
                
                # 如果包含任何登录标识，认为登录成功
                for indicator in login_indicators:
                    if indicator in response_text:
                        logger.info(f"登录状态验证成功 (检测到: {indicator})")
                        return True
                
                # 如果没有重定向到登录页面，也认为登录成功
                if 'login' not in response_text and 'auth' not in response_text:
                    logger.info("登录状态验证成功 (未检测到登录页面)")
                    return True
            
            logger.warning(f"登录状态验证失败 - 状态码: {response.status_code}")
            return False
            
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")
            return False
    
    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """
        获取当前登录用户信息
        
        Returns:
            Dict: 用户信息字典，失败时返回 None
        """
        try:
            response = self.session.get(TOUTIAO_URLS['user_info'], timeout=10)
            
            if response.status_code == 200:
                # 这里需要根据实际的页面结构来解析用户信息
                # 由于今日头条的页面结构可能变化，这里提供一个基本框架
                user_info = {
                    'login_status': True,
                    'user_id': None,
                    'username': None,
                    'nickname': None,
                    'avatar': None,
                    'followers_count': 0,
                    'following_count': 0
                }
                
                logger.info("获取用户信息成功")
                return user_info
            else:
                logger.error(f"获取用户信息失败，状态码: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"获取用户信息异常: {e}")
            return None
    
    def logout(self) -> bool:
        """
        登出当前账户
        
        Returns:
            bool: 登出是否成功
        """
        try:
            # 清除 session 中的 Cookie
            self.session.cookies.clear()
            
            # 删除本地 Cookie 文件
            if Path(self.cookies_file).exists():
                Path(self.cookies_file).unlink()
                
            logger.info("已清除登录信息")
            return True
            
        except Exception as e:
            logger.error(f"登出失败: {e}")
            return False
