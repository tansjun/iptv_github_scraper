# IPTV GitHub Scraper

> 手机提交省份 → GitHub Actions 云端执行 → 自动更新省份 IPTV 源列表

基于原 `iptv_stealth_scraper` 改造的远程执行版本：**安卓手机（GitHub App / HTTP Shortcuts）提交省份 → GitHub Actions 在云端跑爬虫 → 合并后的 `{省份}-live.txt` 自动提交回仓库 → 手机直接取链接导入播放器**。

## 目录结构

```
iptv_github_scraper/
├── IPTVScraper.py              # 主程序（支持 --province / --headless）
├── config.yaml                 # 抓取流程配置（默认地区=海南）
├── requirements.txt            # 依赖
├── .github/workflows/run-iptv.yml  # GitHub Actions 工作流
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

# 指定省份 + 无头模式（模拟 CI）
python IPTVScraper.py --province 湖南 --headless
```

执行结束后根目录生成/更新 `hi-live.txt`（湖南则 `hn-live.txt`），内容为所有下载源合并后的频道列表（跳过文件头两行）。

## 部署到 GitHub（一次性）

1. 在 GitHub 新建一个仓库（如 `iptv-remote`，建议 Private）
2. 推送本项目：
   ```bash
   git init
   git add .
   git commit -m "init"
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
3. 在仓库页 `Actions` 标签 → 左侧 `Run IPTV Scraper` → `Run workflow` → 选择省份 → 绿色按钮，即可手动触发验证一次。

## 安卓手机触发

### 方式一：GitHub App / 浏览器（零安装零配置，最简单）

1. 手机安装 GitHub App（或浏览器访问 `github.com` 登录）
2. 打开你的仓库 → 底部（或页面顶部）`Actions` 标签
3. 左侧选择 `Run IPTV Scraper`
4. 点右侧 `Run workflow` → 下拉选择省份 → 点绿色 `Run workflow` 确认
5. 等 2-5 分钟，切到仓库文件列表，根目录出现 `hi-live.txt` 即完成

> 全程不需要任何额外安装和 Token，只是每次要手动点几下。执行进度可在 Actions 页查看实时日志。

### 方式二：HTTP Shortcuts 一键触发（可选增强，装一个 App）

首次准备：
1. 生成 GitHub Token：GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**
   - Repository access 选本项目仓库；Permissions → Actions 选 **Read and write**、Contents 选 **Read and write**
   - 复制生成的 token（只显示一次）
2. Play 商店安装 [HTTP Shortcuts](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts)（免费）
3. 新建快捷方式：

| 配置项 | 值 |
|---|---|
| 名称 | 触发 IPTV |
| 方法 | `POST` |
| URL | `https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/run-iptv.yml/dispatches` |
| 请求头 | `Authorization: Bearer <你的Token>`、`Accept: application/vnd.github+json` |
| 请求体 | `{"ref":"main","inputs":{"province":"{{省份}}"}}`（`{{省份}}` 为变量，类型选「从列表选择」，填入 34 个省份） |

4. 保存后长按快捷方式 →「添加到主屏幕」，桌面生成图标
5. 之后点桌面图标 → 选省份 → 一键触发（API 返回 204 = 成功）

> Token 只存在 HTTP Shortcuts 里，不要提交到仓库。若提示权限不足，检查 Fine-grained token 的 Actions/Contents 权限。

## 产物获取（导入播放器）

workflow 执行完后，仓库根目录会出现 `hi-live.txt`。手机播放器导入以下任一链接：

- 原始链接：`https://raw.githubusercontent.com/<用户名>/<仓库名>/main/hi-live.txt`
- 加速链接（jsDelivr）：`https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/hi-live.txt`

## 注意事项

1. **云端执行成功率**：GitHub 托管 runner 出口是海外数据中心 IP，访问国内站点可能较慢或被风控；若某次运行频道数为 0 或超时，可重跑一次，或改用自托管 runner（本地常驻）。
2. **每次执行自动清空 `.logs`** 并重新下载合并，仓库只保留最新的 `{省份}-live.txt`。
3. 文件名使用 UTF-8；`{省份}` 无映射时会回退为 `live.txt`。
4. 请遵守目标网站的使用条款，控制执行频率。
