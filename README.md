# IPTV GitHub Scraper

> 手机一键触发 → GitHub Actions 云端执行 → 自动更新省份 IPTV 源列表

基于原 `iptv_stealth_scraper` 改造的远程执行版本：**手机（iOS 快捷指令 / GitHub App）提交省份 → GitHub Actions 在云端跑爬虫 → 合并后的 `{省份}-live.txt` 自动提交回仓库 → 手机直接取链接导入播放器**。

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

## 手机一键触发（iOS 快捷指令）

首次准备：
1. 生成 GitHub Token：GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**（或 classic token 勾选 `repo`）
   - Fine-grained 权限：Repository access 选本项目仓库；Permissions → Actions 选 **Read and write**、Contents 选 **Read and write**
   - 复制生成的 token（只显示一次）
2. 打开 iOS「快捷指令」App，新建快捷指令，依次添加动作：

| 步骤 | 动作 | 配置 |
|---|---|---|
| 1 | 从菜单中选取 | 菜单项：海南、湖南、广东…（按需） |
| 2 | URL | `https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/run-iptv.yml/dispatches` |
| 3 | 获取 URL 内容 | 方法 `POST`；请求头：`Authorization: Bearer <你的Token>`、`Accept: application/vnd.github+json`；请求体（JSON）：`{"ref":"main","inputs":{"province":"海南"}}` |
| 4 | 显示通知 | 标题：IPTV 已触发，正文：省份 海南 |

3. 运行快捷指令 → 手机收到通知即触发成功（API 返回 204 无内容 = 成功）。

> Token 只存在快捷指令里，不要提交到仓库。若提示权限不足，检查 Fine-grained token 的 Actions/Contents 权限。

## 产物获取（导入播放器）

workflow 执行完后，仓库根目录会出现 `hi-live.txt`。手机播放器导入以下任一链接：

- 原始链接：`https://raw.githubusercontent.com/<用户名>/<仓库名>/main/hi-live.txt`
- 加速链接（jsDelivr）：`https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/hi-live.txt`

## 注意事项

1. **云端执行成功率**：GitHub 托管 runner 出口是海外数据中心 IP，访问国内站点可能较慢或被风控；若某次运行频道数为 0 或超时，可重跑一次，或改用自托管 runner（本地常驻）。
2. **每次执行自动清空 `.logs`** 并重新下载合并，仓库只保留最新的 `{省份}-live.txt`。
3. 文件名使用 UTF-8；`{省份}` 无映射时会回退为 `live.txt`。
4. 请遵守目标网站的使用条款，控制执行频率。
