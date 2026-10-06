# polylens-bilibili

读取 B 站视频、评论、弹幕、字幕等公开信息的 MCP 服务，支持用 jq 在返回前筛选和裁剪结果。只读，不向平台写入任何数据。

## 工具

| 工具 | 说明 |
|---|---|
| `search_videos` | 按关键词搜索视频，每批 30 条，最多 30 页 |
| `suggest_keywords` | 搜索框联想词 |
| `get_feed` | 首页推荐流，每批 30 条 |
| `get_up_info` | UP 主资料：签名、粉丝、获赞、等级、认证等 |
| `list_up_videos` | UP 主投稿，每批最多 40 条，可按最新、播放、收藏排序 |
| `get_video_info` | 标题、作者、发布时间、简介与各项统计 |
| `get_parts` | 分 P 清单 |
| `get_danmaku` | 弹幕，按热度取一批，按时间轴返回 |
| `get_comments` | 主评论，热度序或时间序，游标续取 |
| `get_comment_replies` | 某条主评论下的二级评论 |
| `get_subtitles` | 逐句字幕，含起止秒数，可指定语种 |
| `get_frame` | 截取指定时刻的一帧画面 |
| `start_qr_login` · `complete_qr_login` | 扫码登录 |
| `get_login_status` · `logout` | 查询登录状态、退出登录 |

## 登录

`get_comments`、`get_comment_replies`、`get_subtitles`、`get_frame` 需要登录。`start_qr_login` 返回二维码，用 B 站 App 扫码确认后，由 `complete_qr_login` 完成登录。凭据保存在 `~/.cache/polylens-bilibili/cookie`（设置了 `XDG_CACHE_HOME` 时位于其下），`logout` 会删除它。

## 用 jq 精简返回

`search_videos`、`list_up_videos`、`get_feed`、`get_parts`、`get_comments`、`get_comment_replies`、`get_subtitles` 接受可选的 `jq` 参数，在返回前筛选条目或裁剪字段，减少上下文占用。表达式由模型自行编写，例如：

| 用途 | 表达式 |
|---|---|
| 字幕只要文本 | `map(.content) \| join("\n")` |
| 只看第 10 到 15 分钟的字幕 | `[.[] \| select(.start >= 600 and .start < 900)]` |
| 搜索结果只留高播放量 | `[.[] \| select(.view_count > 100000) \| {title, url, view_count}]` |

## 安装

需要 [uv](https://docs.astral.sh/uv/)（会自动准备 Python 3.13+）和 git；`get_frame` 另需 ffmpeg。

```bash
git clone https://github.com/liu-xindi/polylens-bilibili.git
cd polylens-bilibili && uv sync
```

接入 Claude Code：

```bash
claude mcp add polylens-bilibili -- uv run --directory /绝对路径/polylens-bilibili polylens-bilibili
```

接入 Claude Desktop，在 `claude_desktop_config.json` 中加入：

```json
{
  "mcpServers": {
    "polylens-bilibili": {
      "command": "uv",
      "args": ["run", "--directory", "/绝对路径/polylens-bilibili", "polylens-bilibili"]
    }
  }
}
```

升级：`git pull && uv sync`。

## 远程访问（HTTP + OAuth）

供 claude.ai 网页端或手机端以连接器接入。服务以 Streamable HTTP 运行并内置 OAuth，默认只监听本机，需由反向代理以 HTTPS 暴露到公网地址。

```bash
POLYLENS_BILIBILI_TRANSPORT=http \
POLYLENS_BILIBILI_PUBLIC_URL=https://example.com \
POLYLENS_BILIBILI_AUTH_SECRET='<口令>' \
uv run polylens-bilibili
```

然后在 claude.ai 添加连接器，URL 填 `https://example.com/mcp`，首次授权时在同意页输入上面的口令。

| 环境变量 | 命令行 | 默认 | 说明 |
|---|---|---|---|
| `POLYLENS_BILIBILI_TRANSPORT` | `--transport` | `stdio` | `stdio` 或 `http` |
| `POLYLENS_BILIBILI_HTTP_HOST` | `--host` | `127.0.0.1` | 监听地址 |
| `POLYLENS_BILIBILI_HTTP_PORT` | `--port` | `6622` | 监听端口 |
| `POLYLENS_BILIBILI_PUBLIC_URL` | `--public-url` | 无 | 公网地址 |
| `POLYLENS_BILIBILI_AUTH_SECRET` | 无 | 无 | 机主口令 |
| `POLYLENS_BILIBILI_INSECURE_NO_AUTH` | 无 | 否 | 关闭鉴权，仅限本机调试 |

命令行优先于环境变量。http 模式下公网地址和口令缺一则拒绝启动。开启 `INSECURE_NO_AUTH=1` 后任何能连到端口的人都能调用全部工具，包括使用已保存的登录凭据。

## 免责声明与许可证

本工具供个人学习与研究使用。需要登录的功能以使用者本人的凭据，在其账号权限范围内访问，不绕过付费墙或内容保护。所获内容版权归原发布方，使用者须自行遵守法律、平台条款与版权规定，并承担使用后果。

以 [Apache License 2.0](LICENSE) 授权，按现状提供，不附任何担保。
