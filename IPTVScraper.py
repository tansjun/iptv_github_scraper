import csv
import os,time
import asyncio
import yaml
import sqlite3
import random
import re
import argparse
from urllib.parse import quote,unquote,urlparse,parse_qs
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# 省级行政区 → 合并输出文件缩写（34 个省级行政区单一对应）
# 海南=hi、湖南=hn 为用户指定；其余按拼音缩写惯例预填，可直接修改本表
PROVINCE_CODE = {
    "北京": "bj", "天津": "tj", "上海": "sh", "重庆": "cq",
    "河北": "he", "山西": "sx", "辽宁": "ln", "吉林": "jl",
    "黑龙江": "hlj", "江苏": "js", "浙江": "zj", "安徽": "ah",
    "福建": "fj", "江西": "jx", "山东": "sd", "河南": "ha",
    "湖北": "hb", "湖南": "hn", "广东": "gd", "海南": "hi",
    "四川": "sc", "贵州": "gz", "云南": "yn", "陕西": "sn",
    "甘肃": "gs", "青海": "qh", "台湾": "tw",
    "内蒙古": "nmg", "广西": "gx", "西藏": "xz", "宁夏": "nx", "新疆": "xj",
    "香港": "hk", "澳门": "mo",
}

# 合并输出分类规则（按顺序匹配，先命中先归类；未命中归入“其他频道”）
CATEGORY_RULES = [
    ("4K频道", "4K"),
    ("央视频道", "CCTV"),
    ("卫视频道", "卫视"),
]
CATEGORY_ORDER = ["4K频道", "央视频道", "卫视频道", "其他频道"]

# 公告块：固定行 + 每次合并动态填充更新时间（格式与 MY 项目一致）
ANNOUNCE_MAIN_URL = "https://gitlab.com/lr77/IPTV/-/raw/main/%E4%B8%BB%E8%A7%92.mp4"
ANNOUNCE_TIME_URL = "https://gitlab.com/lr77/IPTV/-/raw/main/%E8%B5%B7%E9%A3%8E%E4%BA%86.mp4"

class CooledLock(asyncio.Lock):
    def __init__(self, delay=3, group=None):
        super().__init__()
        self.delay = delay
        self.group=group
        self.group_counter=0
        self.counter=0

    async def __aexit__(self, exc_type, exc, tb):
        """
        重写异步上下文管理器的退出逻辑
        """
        try:
            # 1. 在这里执行强制休眠
            self.counter+=1
            if self.group:
                self.group_counter=(self.group_counter+1)%self.group
            if self.delay > 0 and self.group_counter==0:
                # print(f"[Lock] 释放前强制冷却 {self.delay} 秒...")
                await asyncio.sleep(self.delay*(self.group if self.group else 1) )
            else:
                await asyncio.sleep(self.delay)
            #if self.counter%10==0:
            #    await asyncio.sleep(5)
            #    print("强制等待5秒")
        finally:
            # 2. 无论 sleep 是否被中断，最终必须调用父类的释放逻辑
            # 注意：super().release() 是同步方法
            super().release()
            print(f"[DEBUG] 访问计数器：{self.counter}")
            
class AntiDetectScraper:
    def __init__(self, config_path, province=None):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        self.province_override = province
        # 若通过 CLI --province 指定省份（手机触发核心），覆盖 config 中所有下拉选择步骤的地区值
        if province:
            for step in self.config.get('steps', []):
                if step.get('action_type') == 'interact':
                    for cmd in step.get('interactions', []):
                        if cmd.get('type') == 'select':
                            cmd['value'] = province
        self.db_conn = sqlite3.connect("iptv_data.db")
        self.semaphore = asyncio.Semaphore(self.config.get('max_concurrent_tabs', 3))
        self._init_db()
        self.file_lock = asyncio.Lock()
        self.single_lock = CooledLock(delay=1.5,group=10)
        #self.groupdownload_lock = CooledLock(delay=20,group=3)
        self.max_reteive_pages=5
        self.dispatch_num_failed=0
        self.region_code = self._resolve_region_code()

    def _init_db(self):
        self.db_conn.execute("CREATE TABLE IF NOT EXISTS iptv_result (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT)")
        self.db_conn.commit()

    async def inject_stealth(self, page):
        """深度伪装脚本：隐藏 webdriver，并补齐语言/平台/插件/WebGL/权限/UA-CH 等易被检测的指纹"""
        await page.add_init_script("""
            (() => {
                // ---------- 1. 隐藏 navigator.webdriver（多层防御） ----------
                try { delete Navigator.prototype.webdriver; } catch (e) {}
                try { delete navigator.webdriver; } catch (e) {}
                try {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: Object.assign(() => undefined, {
                            toString: () => 'function get webdriver() { [native code] }'
                        })
                    });
                } catch (e) {}

                // ---------- 2. 平台指纹：UA 声明 Windows，platform 必须一致（无头 Linux 默认报 Linux） ----------
                try {
                    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                } catch (e) {}

                // ---------- 3. 语言指纹 ----------
                try {
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
                } catch (e) {}

                // ---------- 4. 插件伪装（真实 Windows Chrome 有 5 个 PDF 插件，空数组是明显破绽） ----------
                try {
                    const pdfNames = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
                    const list = pdfNames.map((name, i) => ({
                        name: name,
                        description: 'Portable Document Format',
                        filename: 'internal-pdf-viewer-' + (i + 1) + '.dll',
                        length: 0,
                        item: () => null,
                        namedItem: () => null
                    }));
                    list.item = (i) => list[i] || null;
                    list.namedItem = (n) => list.find(p => p.name === n) || null;
                    list.refresh = () => {};
                    Object.defineProperty(navigator, 'plugins', { get: () => list });
                } catch (e) {}

                // ---------- 5. window.chrome 完整对象（真实 Chrome 的属性远比空壳多） ----------
                try {
                    const ev = () => ({ addListener() {}, removeListener() {}, hasListener() {} });
                    window.chrome = {
                        app: {
                            isInstalled: false,
                            InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                            RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                            getDetails() { return {}; },
                            getIsInstalled() {},
                            getManifest() { return {}; }
                        },
                        csi() { return {}; },
                        loadTimes() { return {}; },
                        runtime: {
                            OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                            OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                            PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                            PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                            PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                            RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
                            connect() {},
                            sendMessage() {},
                            getManifest() { return {}; },
                            id: undefined
                        },
                        webstore: {
                            onInstallStageChanged: ev(),
                            onDownloadProgress: ev(),
                            install() {}
                        }
                    };
                } catch (e) {}

                // ---------- 6. permissions.query 伪装（无头常被返回 denied，真人浏览器为 prompt/granted） ----------
                try {
                    if (window.navigator.permissions && window.navigator.permissions.query) {
                        const orig = window.navigator.permissions.query.bind(window.navigator.permissions);
                        window.navigator.permissions.query = (params) => {
                            if (params && params.name === 'notifications') {
                                return Promise.resolve({ state: Notification.permission || 'prompt', onchange: null });
                            }
                            return orig(params);
                        };
                    }
                } catch (e) {}

                // ---------- 7. WebGL 渲染器伪装（无头模式返回 SwiftShader 是最强检测信号之一） ----------
                try {
                    const VENDOR = 'Google Inc. (NVIDIA)';
                    const RENDERER = 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                    const patchGL = (gl) => {
                        if (!gl || gl.__patched) return;
                        gl.__patched = true;
                        const origParam = gl.getParameter.bind(gl);
                        const origExt = gl.getExtension.bind(gl);
                        gl.getParameter = function (p) {
                            const v = Number(p);
                            if (v === 37445) return VENDOR;   // UNMASKED_VENDOR_WEBGL
                            if (v === 37446) return RENDERER; // UNMASKED_RENDERER_WEBGL
                            return origParam(p);
                        };
                        gl.getExtension = function (name) {
                            if (String(name) === 'WEBGL_debug_renderer_info') {
                                return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
                            }
                            return origExt(name);
                        };
                    };
                    const origGetContext = HTMLCanvasElement.prototype.getContext;
                    HTMLCanvasElement.prototype.getContext = function (type, ...args) {
                        const ctx = origGetContext.call(this, type, ...args);
                        if (ctx && type && String(type).indexOf('webgl') === 0) patchGL(ctx);
                        return ctx;
                    };
                } catch (e) {}

                // ---------- 8. UA-CH（User-Agent Client Hints）平台对齐（无头 Linux 默认 platform=Linux） ----------
                try {
                    if (navigator.userAgentData) {
                        Object.defineProperty(navigator.userAgentData, 'platform', { get: () => 'Windows' });
                        const origHigh = navigator.userAgentData.getHighEntropyValues.bind(navigator.userAgentData);
                        navigator.userAgentData.getHighEntropyValues = async (hints) => {
                            const res = await origHigh(hints);
                            if (res) {
                                res.platform = 'Windows';
                                res.platformVersion = '15.0.0';
                                res.architecture = 'x86';
                                res.bitness = '64';
                            }
                            return res;
                        };
                    }
                } catch (e) {}
            })();
        """)

    async def run(self):
        self._clear_logs()  # 每次执行先清空 .logs 目录旧文件
        async with async_playwright() as p:
            # GitHub Actions 等无显示服务器环境需无头模式，本地可用 PLAYWRIGHT_HEADLESS 控制
            headless = os.environ.get("PLAYWRIGHT_HEADLESS", "0").lower() in ("1", "true", "yes")
            print(f"[DEBUG] 浏览器模式: {'headless' if headless else 'headed'}")
            browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            s = self.config['stealth_settings']
            # locale/timezone 必须传入 context：影响 Accept-Language 请求头、Intl API 和 Date 时区指纹
            # （config.yaml 已配置这两项，此前未生效）
            context = await browser.new_context(
                user_agent=s['user_agent'],
                viewport=s['viewport'],
                locale=s.get('locale', 'zh-CN'),
                timezone_id=s.get('timezone', 'Asia/Shanghai'),
            )
            page = await context.new_page()
            
            for step in self.config['steps']:
                print(f"\n[DEBUG] === 开始执行步骤: {step['name']} ===")
                try:
                    await self.inject_stealth(page) # 每次操作前确保注入
                    
                    if step.get('wait_for_navigation'):
                        print(f"[DEBUG] 期待页面跳转中...")
                        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
                            await self.execute_step_logic(page, context, step)
                    else:
                        await self.execute_step_logic(page, context, step)
                    
                    print(f"[DEBUG] 步骤 {step['name']} 执行完毕。")
                    await asyncio.sleep(random.uniform(1, 2))
                except Exception as e:
                    print(f"[ERROR] 步骤 {step['name']} 发生异常: {str(e)}")
                    import traceback
                    trackmsg = traceback.format_exc()
                    print(trackmsg)
                    # 打印当前URL辅助调试
                    print(f"[DEBUG] 当前页面URL: {page.url}")
                    break 

            await browser.close()

        self._merge_logs()  # 合并 .logs 全部 txt（跳过前两行）→ 根目录 {code}-live.txt
            

    async def execute_step_logic(self, page, context, step):
        a_type = step['action_type']
        print(f"[DEBUG] 动作类型: {a_type}")

        if a_type == "navigate":
            print(f"[DEBUG] 正在访问: {step['url']}")
            async with self.single_lock:
                response = await page.goto(step['url'])
                if response.status >=400:
                    print(f"[WARN] http_code: {response.status}")
                            
        elif a_type == "interact":
            for cmd in step.get('interactions', []):
                act_type = cmd.get('type',None)
                selector = cmd.get('selector')
                print(f"[DEBUG] 交互操作: {act_type} -> {selector} = {cmd.get('value')}")
                
                if act_type == "select":
                    response = await page.select_option(selector, label=cmd['value'])
                elif act_type == "click":
                    response = await page.click(selector)
                elif act_type == "download":
                    # 1. 找到元素并获取原始 URL
                    loc = page.locator(selector).first
                    await loc.wait_for(state="attached", timeout=5000)
                    
                    download_url = await loc.get_attribute("href")
                    if not download_url:
                        print(f"[WARN] 未找到下载链接: {selector}")
                        continue
                        
                    custom_rules = cmd.get('custom_rules')
                    kwargs = {"download_url": download_url,
                              "item_id":f"{cmd.get('save_prefix','iptvlist')}-{step.get('temp_item_id','')}-",
                             }
                    for rule in custom_rules:
                        cmd_str = rule['action']
                        custom_handler = getattr(self, cmd_str, None)
                        if custom_handler:
                            updates=custom_handler(page=page,context=context,step=step,**kwargs)
                            for key, val in updates.items():
                                if key in kwargs: kwargs[key] = val 

                    success=True
                    print(f"[DEBUG] 准备下载文件: {kwargs['download_url']}")
                    async with self.single_lock:
                        # 3. 执行下载请求
                        # 推荐方式：使用 page.expect_download() 配合简单跳转，# 或者直接使用 context.request 发起请求（更适合静默下载）
                        success = await self.download_via_request(page, kwargs['download_url'], item_id=kwargs['item_id'])
                        #success = await self.download_via_goto(page, kwargs['download_url'], item_id=kwargs['item_id'])
                       
                    if not success:
                        raise Exception(f"[{kwargs['item_id']}] download_via_request ERROR!")
                
                wait_after = cmd.get('wait_after',0)
                if wait_after>0:
                    await asyncio.sleep(wait_after)    
        elif a_type == "dispatch_tabs":
            print(f"[DEBUG] 进入分发逻辑，等待选择器: {step['table_selector']}")
            await self.handle_dispatch(context, page, step)
            print(f"[DEBUG] 分发失败数:{self.dispatch_num_failed}")
        elif a_type == "extract_table":
            print(f"[DEBUG] extract_table")
            await self.handle_table_extraction(new_page, t_step)


    async def handle_dispatch(self, context, page, step):
        # 1. 定位到目标 section 和 table
        #container = page.locator("body > div.container > section.group-section").nth(1)
        #rows = await container.locator("table.iptv-table tr:not(:first-child)").all()
        rows= await page.locator(step['table_selector']+ " tr").all()
        tasks = []
        for row in rows:
            status_text = await row.inner_text()#;print(status_text)
            if step['filter_condition']['exclude'] in status_text:
                continue
                
            # 获取 onclick 属性的内容
            link_el = row.locator("td a")
            if await link_el.count() == 0: continue
            
            onclick_text = await link_el.get_attribute("onclick")
            
            # 使用正则从 onclick="gotoIP('3p40X...', 'multicast')" 中提取参数
            # 对应代码：gotoIP(_0x5e3823, _0x2d65fd)
            #match = re.search(r"gotoIP\('([^']+)',\s*'([^']+)'\)", onclick_text)
            match = re.search(rf'{step["onclick_js"]}', onclick_text)
            
            if match:
                ip_id = match.group(1)
                ip_type = match.group(2)
                ip = await link_el.inner_text()
                ip = ip.strip()
                
                # 模拟 JS 里的拼接逻辑: index.php?p=xxx&type=xxx
                # 这里的 quote 对应 JS 的 encodeURIComponent
                detail_url = "https://iptv.cqshushu.com/index.php?_js=1"#f"https://iptv.cqshushu.com/index.php?p={quote(ip_id)}&type={quote(ip_type)}"
                
                # 将生成的 URL 分发到 worker 异步处理
                
                tasks.append(asyncio.create_task(self.url_worker(context, detail_url,ip_id,ip_type,ip, tab_steps=step['tab_steps'])))
                
                task_count = len(tasks)
                if task_count >= self.max_reteive_pages:
                    break
                
                # 错峰启动：间隔 0.8~1.8s 随机创建下一个任务，规避站点 JS 的 1000ms 频率检测
                # （冷却锁只约束页面访问时刻，任务本身的创建节奏也要随机化）
                await asyncio.sleep(random.uniform(0.8, 1.8))
                  
        if tasks:
            self.dispatch_num_failed=len(tasks)
            await asyncio.gather(*tasks)
        
        '''
        # 3. 按步长切片循环
        batch_size = self.config.get('max_concurrent_tabs', 3)
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i : i + batch_size]
            print(f"[BATCH] 正在启动第 {i//batch_size + 1} 批次任务，规模: {len(batch)}")
            time0= time.time()
            # 这一行会阻塞，直到这 4 个任务全部完成
            await asyncio.gather(*batch)
            if i+batch_size<len(tasks):
                sleep_time = max(20 - (time.time()-time0),3)            
                print(f"sleep {sleep_time}")
                await asyncio.sleep(sleep_time)
        '''
            
    
    async def url_worker(self, context,url, item_id, item_type, ip, tab_steps):
        """
        通过克隆首页环境并执行原生 JS 函数来规避反爬的并发工作者
        """
        async with self.semaphore:
            # 1. 创建新页面
            new_page = await context.new_page()
            
            # 2. 注入反爬指纹伪装
            # 必须在 goto 之前注入，确保 window.webdriver 等属性被抹除
            await self.inject_stealth(new_page)
            
            try:
                print(f"[DEBUG] 正在为 ID: {item_id} 准备环境...")
                
                # 3. 设置必要的 Header 模拟
                # 确保 Referer 指向主页，模拟同源行为
                await new_page.set_extra_http_headers({
                    "Referer": url,
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document"
                })

                # 4. 先访问主页加载基础 JS 环境（包含 pabe06.js 的加载）
                # wait_until="networkidle" 确保所有混淆的 JS 已经执行完毕，gotoIP 函数已定义
                    
                retry=0
                page_timeout=False
                while retry<3:
                    async with self.single_lock:
                        if retry==0 or page_timeout:
                            response =await new_page.goto( url, wait_until="domcontentloaded",  timeout=30000 )
                            page_timeout=False
                            if retry>0:
                                await asyncio.sleep(2)
                        if retry>0:
                            response =await new_page.reload( wait_until="domcontentloaded",  timeout=30000 )
                        
                    retry+=1
                    if response.status == 429 or response.status==403:
                        if retry <3:
                            print(f"[WARN] HTTP-CODE:{response.status},[{item_id}]访问过于频繁，wait 20s后再试。重试{retry}")
                            await asyncio.sleep(12*retry + random.uniform(2, 8))
                            continue
                        else:
                            print(f"[ERROR] [{item_id}]超过重试次数。")
                            return
                    
                    
                    # 5. 执行页面原生函数触发跳转
                    # 这样跳转会发生在 new_page 内部，且完全继承首页的 Cookie 和 JS 变量
                    print(f"[DEBUG] 触发原生函数跳转: gotoIP('{item_id}', '{item_type}')")
                    
                    async with self.single_lock:
                        # 等待混淆 JS（pabe06.js 等）执行完毕，gotoIP 函数就绪
                        # domcontentloaded 只保证 DOM 解析完成，异步脚本可能尚未执行完
                        goto_ready = False
                        try:
                            await new_page.wait_for_function("typeof gotoIP !== 'undefined'", timeout=20000)
                            goto_ready = True
                        except Exception as e:
                            print(f"[WARN] [{item_id}] gotoIP 未就绪（{type(e).__name__}），改用直接 URL 跳转兜底")

                        if goto_ready:
                            # 参数走 Playwright 通道，避免字符串拼接注入
                            await new_page.evaluate("([id, type]) => gotoIP(id, type)", [item_id, item_type])
                        else:
                            # 兜底：等价于 gotoIP 内部的跳转逻辑 index.php?p=xxx&t=xxx
                            # （实测 gotoIP 跳转后的 URL 为 ?p=<id>&t=<type>，注意参数名是 t）
                            fallback_url = f"https://iptv.cqshushu.com/index.php?p={quote(item_id)}&t={quote(item_type)}"
                            fb_resp = await new_page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                            if fb_resp and fb_resp.status in (429, 403):
                                print(f"[WARN] [{item_id}] 兜底URL HTTP-CODE:{fb_resp.status}，重试 {retry}")
                                page_timeout = True
                                await asyncio.sleep(12 * retry + random.uniform(2, 8))
                                continue
                        
                        # 6. 等待跳转后的详情页加载完成
                        # 我们等待详情页特有的元素出现，比如“查看频道列表”按钮或 controls 区域
                        await new_page.wait_for_load_state("networkidle")
                        
                        # 假设详情页有 controls 类
                        await new_page.wait_for_selector("div.controls", timeout=15000)
                        print(f"[SUCCESS] [{item_id}]详情页加载成功: {new_page.url}")
                        textinfo = await new_page.locator("div.controls").inner_text()
                        if '阿爬' in textinfo:
                            print(f"[ERROR] [{item_id}]，阿爬，wait 20s后重试：{retry}")
                            page_timeout=True
                            await asyncio.sleep(15*retry + random.uniform(2, 8))
                            continue
                                        

                    # 7. 执行后续的业务逻辑（例如提取频道列表表格）
                    for t_step in tab_steps:
                        print(f"[DEBUG] 执行子步骤: {t_step['name']}")
                        # 调用你定义的通用执行逻辑
                        try:
                            t_step['temp_item_id']=ip
                            await self.execute_step_logic(new_page, context, t_step)
                        except Exception as e:
                            print(f"[ERROR] [{item_id}]，wait 20s后重试：{retry}\n{e}")
                            page_timeout=True
                            await asyncio.sleep(12*retry + random.uniform(2, 8))
                            break
                    if not page_timeout:
                        async with self.file_lock: 
                            self.dispatch_num_failed-=1
                        break
                    
            except Exception as e:
                # 失败现场快照：截图 + 标题 + URL + 正文片段，便于定位是被反爬拦截还是页面结构变化
                try:
                    await new_page.screenshot(path=f".logs/error_{item_id}.png", full_page=True)
                    diag_title = await new_page.title()
                    diag_body = await new_page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                    diag_body = diag_body.replace('\n', ' | ')
                    print(f"[DIAG] [{item_id}] URL={new_page.url}")
                    print(f"[DIAG] [{item_id}] TITLE={diag_title}")
                    print(f"[DIAG] [{item_id}] BODY={diag_body}")
                except Exception as de:
                    print(f"[DIAG] [{item_id}] 现场快照失败: {de}")
                print(f"[ERROR] 处理任务 {item_id} 时发生异常: {e}")
                import traceback
                trackmsg = traceback.format_exc()
                print(trackmsg)
                await asyncio.sleep(15)
            finally:
                # 完成后关闭标签页，释放内存
                await asyncio.sleep(1)
                await new_page.close()
                
            
                
    async def handle_dispatch0(self, context, page, step):
        # 1. 定位表格和行
        await page.wait_for_selector(step['table_selector'])
        rows = await page.query_selector_all(f"{step['table_selector']} tr")
        
        # 2. 解析表头索引
        headers = [(await c.inner_text()).strip() for c in await rows[0].query_selector_all("th, td")]
        status_idx = next(i for i, h in enumerate(headers) if step['filter_condition']['column'] in h)
        click_idx = next(i for i, h in enumerate(headers) if step['click_column'] in h)

        # 3. 收集所有待点击的元素 (注意：不要在循环里直接执行异步任务，先收集)
        target_locators = []
        for row in rows[1:]:
            print(await row.inner_text())
            cells = await row.query_selector_all("td")
            status_text = await cells[status_idx].inner_text()
            if step['filter_condition']['exclude'] not in status_text:
                link_el = await cells[click_idx].query_selector("a")
                if link_el:
                    target_locators.append(link_el)

        print(f"[DEBUG] 准备并发处理 {len(target_locators)} 个 JS 跳转任务")

        # 2. 核心修正：不要直接 gather 列表推导式，手动分发任务
        tasks = []
        for el in target_locators:
            # 创建异步任务
            task = asyncio.create_task(self.tab_worker_by_click(context, el, step['tab_steps']))
            tasks.append(task)
            
            # 关键：每启动一个新标签页的任务，强制等待 200-500ms
            # 这给浏览器留出了处理新窗口创建的时间，防止点击丢失
            await asyncio.sleep(random.uniform(0.2, 0.5))

        # 3. 等待所有并发任务完成
        if tasks:
            await asyncio.gather(*tasks)
            print("[DEBUG] 所有子标签页任务已执行完毕")

    
    async def tab_worker(self, context, link_element, tab_steps):
        async with self.semaphore:
            async with context.expect_page() as page_info:
                await link_element.click()
            new_page = await page_info.value
            print(f"[DEBUG] 新标签页已开启: {new_page.url}")
            
            await self.inject_stealth(new_page)
            try:
                for t_step in tab_steps:
                    print(f"[DEBUG]   Tab执行子步骤: {t_step['name']}")
                    await self.execute_step_logic(new_page, context, t_step)
                    if t_step['action_type'] == "extract_table":
                        await self.handle_table_extraction(new_page, t_step)
            finally:
                await new_page.close()

    async def download_via_goto(self, page, download_url, item_id,save_dir=".logs"):
        """
        使用 context.request 静默下载文件
        """
        try:
            async with page.expect_download() as download_info:
                # 强行让页面跳转到下载地址，触发浏览器下载行为
                await page.goto(download_url)
            
            download = await download_info.value
            
            # 4. 保存文件
            # 获取文件名，如果配置了前缀则使用前缀
            if item_id:
                save_name=item_id
            else:
                save_name = download.suggested_filename
            save_path = os.path.join(save_dir, save_name)
            
            # 确保目录存在
            os.makedirs(save_dir, exist_ok=True)
            
            await download.save_as(save_path)
            print(f"[SUCCESS] 文件已保存至: {save_path}")

            # 下载完后返回上一页，因为 goto 改变了当前页地址
            await page.go_back()
            await page.wait_for_load_state("networkidle")
            return True
        except Exception as e:
            print(f"[ERROR] 下载过程中出错: {e}")
        return False
    
    async def download_via_request(self, page, download_url, item_id, save_dir=".logs"):
        """
        使用 context.request 静默下载文件
        """
        # 获取当前的 request 上下文
        request_context = page.context.request
        
        try:
            print(f"[DEBUG] 发起 API 下载请求: {download_url}")
            # 发起 GET 请求
            # 注意：如果服务器验证 Referer，确保在这里显式加上
            response = await request_context.get(
                download_url,
                headers={
                    "Referer": page.url, # 自动设置为当前详情页的 URL
                    "Accept": "text/plain,application/octet-stream,*/*"
                }
            )

            if response.status == 200:
                # 读取二进制流
                content = await response.body()
                headers = response.headers
                
                # 确保保存目录存在
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)

                # 构造文件名（建议使用 item_id 确保唯一性）
                filename= f"{item_id}.txt"
                cd_value = headers.get('content-disposition', '')
                if cd_value:
                    # 兼容 filename="name.txt" 和 filename=name.txt 两种格式
                    match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd_value)
                    if match:
                        # 处理可能的 URL 编码（如 %E4%BD%A0%E5%A5%BD.txt）
                        filename = unquote( match.group(1)).replace(':','-')
                    
                file_path = os.path.join(save_dir, filename)
                
                with open(file_path, "wb") as f:
                    f.write(content)
                
                print(f"[SUCCESS] 文件下载成功，保存至: {file_path}")
                return True
            else:
                print(f"[ERROR] [{item_id}]下载请求失败，状态码: {response.status}")
                return False

        except Exception as e:
            print(f"[ERROR] [{item_id}]执行 context.request 下载时出错: {e}")
            return False
            
    async def parse_table(self, page, table_selector):
        """
        解析 HTML 表格并返回字典列表
        :param table_selector: 目标表格的选择器
        :return: List[dict]
        """
        try:
            # 确保表格已加载
            table = page.locator(table_selector)
            await table.wait_for(state="visible", timeout=10000)

            # 1. 提取所有行
            rows = await table.locator("tr").all()
            if not rows:
                print(f"[WARN] 选择器 {table_selector} 未找到任何行")
                return []

            # 2. 提取表头 (第一行)
            header_cells = await rows[0].locator("th, td").all()
            headers = []
            for cell in header_cells:
                text = (await cell.inner_text()).strip()
                # 过滤掉空的表头或换行符
                headers.append(text if text else f"column_{header_cells.index(cell)}")
            
            print(f"[DEBUG] 提取到表头: {headers}")

            # 3. 提取数据行 (从第二行开始)
            results = []
            for row in rows[1:]:
                cells = await row.locator("td").all()
                # 如果单元格数量与表头不匹配，可能是特殊行（如合并单元格），跳过
                if len(cells) != len(headers):
                    continue
                
                row_data = {}
                for i, cell in enumerate(cells):
                    row_data[headers[i]] = (await cell.inner_text()).strip()
                
                results.append(row_data)

            return results

        except Exception as e:
            print(f"[ERROR] 解析表格失败: {e}")
            return []
    
    async def handle_table_extraction(self, page, step):
        """
        支持自动翻页的数据抓取函数
        """
        all_data = []
        page_num = 1
        
        while True:
            print(f"[DEBUG] 正在解析第 {page_num} 页数据...")
            
            # 1. 解析当前页表格
            # 解析当前页
            current_page_data = await self.parse_table(page, step['table_selector'])
            if current_page_data:
                await self.save_data_batch(current_page_data, step)

            next_button = page.locator("a.pagination-btn:has-text('下一页')")
            
            # 判断是否可点击（注意：XHR 翻页有时会给按钮加 'disabled' class）
            is_disabled = "disabled" in (await next_button.get_attribute("class") or "")
            if not await next_button.is_visible() or is_disabled:
                break

            print(f"[DEBUG] 翻页中，当前页码: {page_num}")

            try:
                # 针对 XHR 的精准等待
                async with page.expect_response(lambda res: "page=" in res.url and res.status == 200):
                    await next_button.click()
                
                # 关键：XHR 返回后，等待 DOM 渲染完成
                # networkidle 表示 500ms 内不再有网络请求，通常意味着渲染已就绪
                await page.wait_for_load_state("networkidle")
                
                # 额外保险：等待表格的第一行是可见的
                await page.locator(step['table_selector']).locator("tr").first.wait_for(state="visible")
                
                page_num += 1
                await asyncio.sleep(random.uniform(1, 2)) # 礼貌延迟
            except Exception as e:
                print(f"[ERROR] 翻页超时或失败: {e}")
                break

        print(f"[SUCCESS] 任务结束，共抓取 {page_num} 页，总计 {len(all_data)} 条记录。")

    async def save_data_batch(self, data, step):
        """
        复用之前的 CSV 和 数据库保存逻辑
        """
        async with self.file_lock:
            # CSV 逻辑
            csv_path = step.get('csv_output', 'iptv_channels.csv')
            file_exists = os.path.isfile(csv_path)
            with open(csv_path, mode='a', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=data[0].keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerows(data)
            
            # DB 逻辑... (此处省略，同前文)
        # --- 数据库保存逻辑 ---
        try:
            cursor = self.db_conn.cursor()
            for row in data:
                # 动态生成 SQL (注意：生产环境建议预定义表结构)
                keys = ", ".join(row.keys())
                placeholders = ", ".join(["?"] * len(row))
                sql = f"INSERT INTO iptv_result ({keys}) VALUES ({placeholders})"
                cursor.execute(sql, list(row.values()))
            self.db_conn.commit()
        except sqlite3.Error as e:
            print(f"[DB ERROR] 写入数据库失败: {e}")
    
    def custom_process_download(self, *args, **kwargs):
        print(f"[DEBUG] call custom func:custom_process_download {len(kwargs)}")
        
        original_href = kwargs.get('download_url')
        page = kwargs.get('page')
        item_id = kwargs.get('item_id')
        # 2. 构造下载 URL
        # 处理相对路径：如果 href 以 ? 开头，需要拼接到当前基础 URL
        if original_href.startswith('?'):
            base_url,parms = page.url.split('?')
            download_url = base_url + original_href
            if parms:
                dd=parse_qs(urlparse(original_href).query)
                if 's' in dd.keys():
                    item_id+=dd['s'][0]
        else:
            download_url = original_href

        # 拼接参数：&channels=1&download=txt
        if '?' in download_url:
            download_url += "&channels=1&download=txt"
        else:
            download_url += "?channels=1&download=txt"
        return {'download_url':download_url ,'item_id':item_id}
        

    def _categorize_channel(self, name):
        """按分类规则顺序判断频道名称所属类别（4K → 央视 → 卫视 → 其他）"""
        up = (name or "").strip().upper()
        for cat, keyword in CATEGORY_RULES:
            if keyword in up:
                return cat
        return "其他频道"

    def _resolve_region_code(self):
        """省份缩写解析：优先 CLI --province，其次 config 的 select 地区值"""
        region_name = self.province_override
        if not region_name:
            for step in self.config.get('steps', []):
                if step.get('action_type') == 'interact':
                    for cmd in step.get('interactions', []):
                        if cmd.get('type') == 'select':
                            region_name = cmd.get('value')
                            break
        if not region_name:
            print("[WARN] config 中未找到地区下拉选择，合并输出将命名为 live.txt")
            return None
        for name, code in PROVINCE_CODE.items():
            if name in region_name:
                print(f"[DEBUG] 地区「{region_name}」→ 省份缩写 {code}")
                return code
        print(f"[WARN] 地区「{region_name}」未命中省份映射表，合并输出将命名为 live.txt")
        return None

    def _clear_logs(self, log_dir=".logs"):
        """每次执行前清空 .logs 目录中的旧文件"""
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            return
        for name in os.listdir(log_dir):
            path = os.path.join(log_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    print(f"[DEBUG] 已清空旧文件: {name}")
                except OSError as e:
                    print(f"[WARN] 清理 {name} 失败: {e}")

    def _merge_logs(self, log_dir=".logs", output_name=None):
        """合并 .logs 中所有 txt（跳过头部两行），输出到根目录 {code}-live.txt"""
        if output_name is None:
            output_name = f"{self.region_code}-live.txt" if self.region_code else "live.txt"
        if not os.path.isdir(log_dir):
            print(f"[WARN] {log_dir} 目录不存在，跳过合并")
            return
        txt_files = sorted(f for f in os.listdir(log_dir) if f.lower().endswith(".txt"))
        if not txt_files:
            print(f"[WARN] {log_dir} 中没有 txt 文件，跳过合并")
            return
        merged = []
        for fname in txt_files:
            path = os.path.join(log_dir, fname)
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    raw_lines = fh.read().splitlines()
            except Exception as e:
                print(f"[WARN] 读取 {fname} 失败，跳过: {e}")
                continue
            data_lines = [ln.strip() for ln in raw_lines[2:] if ln.strip()]
            merged.extend(data_lines)
            print(f"[DEBUG] 合并 {fname}: 有效行 {len(data_lines)}（文件共 {len(raw_lines)} 行）")
        if not merged:
            print("[WARN] 合并结果为空，不生成输出文件")
            return

        # 按类别分组（顺序：4K → 央视 → 卫视 → 其他）
        buckets = {cat: [] for cat in CATEGORY_ORDER}
        for line in merged:
            name = line.split(",", 1)[0]
            buckets[self._categorize_channel(name)].append(line)

        # 组装输出：公告块 + 各分类块（分类为空则跳过该分类头）
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        output = [
            "公告,#genre#",
            f"更新日期,{ANNOUNCE_MAIN_URL}",
            f"{now},{ANNOUNCE_TIME_URL}",
        ]
        for cat in CATEGORY_ORDER:
            if buckets[cat]:
                output.append(f"{cat},#genre#")
                output.extend(buckets[cat])

        base_dir = os.path.dirname(os.path.abspath(__file__))
        out_path = os.path.join(base_dir, output_name)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(output) + "\n")
        print(f"[SUCCESS] 已合并 {len(txt_files)} 个文件共 {len(merged)} 行 → {out_path}")
        for cat in CATEGORY_ORDER:
            print(f"[INFO] {cat}: {len(buckets[cat])} 条")

    def test(self):
        cmd_str='custom_process_download'
        custom_handler = getattr(self, cmd_str, None)
        kwargs = {'ab':9988}#{'page':2,'context':1,'step':3,'download_url':'htt:121231321'}
        page=2
        context=3
        download_url='http1212122112'
        custom_handler(page=page,context=context,download_url=download_url,**kwargs)
            
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IPTV 源抓取（支持 GitHub Actions + 手机触发）")
    parser.add_argument("--province", default=None, help="省份名称，覆盖 config 中的地区选择（如 海南、湖南）")
    args = parser.parse_args()
    scraper = AntiDetectScraper('config.yaml', province=args.province)
    #kwargs = {'page':2,'context':1,'step':3,'download_url':'htt:121231321'}
    #scraper.test()
    asyncio.run(scraper.run())