# IPTV GitHub Scraper

> 手机提交省份 → GitHub Actions 云端执行 → 自动更新分类 IPTV 源列表

基于原 `iptv_stealth_scraper` 改造的远程执行版本：**安卓手机（GitHub App / HTTP Shortcuts）提交省份 → GitHub Actions 在云端跑爬虫 → 合并后的 `{省份}-live.txt`（按 4K/央视/卫视/其他分类）自动提交回仓库 → 手机直接取链接导入播放器**。

抓取与伪装逻辑与本地调通版 `IPTVSCR` 一致（深度指纹伪装、gotoIP 兜底跳转、失败现场快照），差异仅为**通过手机 HTTP Shortcuts 提交省份触发**。

## 目录结构

```
iptv_github_scraper/
├── IPTVScraper.py              # 主程序（支持 --province）
├── config.yaml                 # 抓取流程配置（默认地区=海南）
├── requirements.txt            # 依赖（playwright 固定 1.48.0，勿升级）
├── .github/workflows/
│   ├── run-iptv.yml            # 抓取工作流（手机触发入口，成功后自动部署 Pages）
│   └── static.yml              # GitHub Pages 手动部署入口（备用）
└── README.md
```

## 省份缩写规则（34 个省级行政区单一对应）

| 地区 | 缩写 | 地区 | 缩写 |
|---|---|---|---|
| 海南 | `hi` | 湖南 | `hn` |
| 北京 | `bj` | 广东 | `gd` |
| 上海 | `sh` | 河南 | `ha` |
| 浙江 | `zj` | 四川 | `sc` |
| ... | ... | ... | ... |

完整映射见 `IPTVScraper.py` 顶部 `PROVINCE_CODE`，可直接修改。

## 本地运行

```bash
pip install -r requirements.txt
python -m playwright install chromium

# 本地有头模式（默认地区=config 里的海南）
python IPTVScraper.py

# 指定省份（如湖南）
python IPTVScraper.py --province 湖南

# 无头模式（模拟 CI/服务器）
PLAYWRIGHT_HEADLESS=1 python IPTVScraper.py --province 湖南
```

执行结束后根目录生成/更新 `hn-live.txt`，内容为分类合并的频道列表：公告块 + `4K频道`/`央视频道`/`卫视频道`/`其他频道` 分组（`#genre#` 格式，播放器可识别分类）。

## 部署到 GitHub（一次性）

1. 在 GitHub 新建一个仓库（建议 Private）
2. 推送本项目：
   ```bash
   git remote add origin https://github.com/<用户名>/<仓库名>.git
   git push -u origin master
   ```
3. 在仓库页 `Actions` 标签 → 左侧 `Run IPTV Scraper` → `Run workflow` → 选择省份 → 绿色按钮，先手动触发验证一次。
4. （一次性）启用 GitHub Pages：仓库 Settings → Pages → Source 选 `Deploy from a branch` → 分支选 `gh-pages` → Save。此后**每次抓取成功会自动部署到 Pages**（部署已并入主 workflow，无需手动触发）。

## 安卓手机触发

### 方式一：GitHub App / 浏览器（零安装零配置，最简单）

1. 手机安装 GitHub App（或浏览器访问 `github.com` 登录）
2. 打开你的仓库 → `Actions` 标签 → 左侧 `Run IPTV Scraper`
3. 点 `Run workflow` → 下拉选择省份 → 绿色确认
4. 等 2-5 分钟，仓库根目录出现 `hi-live.txt` 即完成

### 方式二：HTTP Shortcuts 一键触发（推荐）

1. 生成 GitHub Token：Settings → Developer settings → Personal access tokens → **Fine-grained tokens**
   - Repository access 选本项目仓库；Permissions → Actions 选 **Read and write**、Contents 选 **Read and write**
2. Play 商店安装 [HTTP Shortcuts](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts)
3. 新建快捷方式：

| 配置项 | 值 |
|---|---|
| 名称 | 触发IPTV |
| 方法 | `POST` |
| URL | `https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/run-iptv.yml/dispatches` |
| 请求头 | `Authorization: Bearer <Token>`、`Accept: application/vnd.github+json` |
| 请求体 | `{"ref":"master","inputs":{"province":"湖南"}}`（可把省份写成固定值，或改用变量） |

4. 保存 → 添加到主屏幕 → 点图标一键触发（API 返回 204 = 成功）

> 注意：`ref` 必须是你仓库的实际分支名（`master` 或 `main`，以仓库默认分支为准）；`province` 必须是 34 省全名之一。Token 只存 App 里，勿提交到仓库。

## 产物获取（导入播放器）

workflow 执行完后，仓库根目录出现 `{省份}-live.txt`，可用以下任一链接导入播放器：

- 原始链接：`https://raw.githubusercontent.com/<用户名>/<仓库名>/master/hn-live.txt`
- 加速链接（jsDelivr）：`https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@master/hn-live.txt`
- Pages 链接（推荐）：`https://<用户名>.github.io/<仓库名>/hn-live.txt` —— 每次抓取成功后自动更新

## 注意事项

1. **Playwright 版本必须固定 1.48.0**（requirements.txt 已锁定）：1.49+ 无头模式改用独立 `chrome-headless-shell` 二进制，指纹易被目标站识别，海外 IP 下会导致任务全部失败。勿随意升级。
2. **云端执行成功率**：GitHub 托管 runner 出口是海外数据中心 IP，访问国内站点可能较慢或被风控。脚本已内置深度伪装、gotoIP 兜底跳转、3 次自动重试和失败现场快照，失败时可在 Actions 日志看到 `[DIAG]` 诊断信息。
3. 每次执行自动清空 `.logs` 并重新下载合并，仓库只保留最新 `{省份}-live.txt`。
4. `{省份}` 无映射时会回退为 `live.txt`。
5. 请遵守目标网站的使用条款，控制执行频率。
