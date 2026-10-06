# polylens-bilibili

用于读取 B 站公开视频的相关信息的 MCP 服务。

## 工具

| 工具 | 说明 | 登录 |
|---|---|---|
| `search_videos` | 按关键词搜索视频，每批 30 条，最多翻 30 页 | 否 |
| `suggest_keywords` | 搜索框联想，输入关键词给出建议词 | 否 |
| `list_up_videos` | UP 主的投稿，每批最多 40 条，可按最新、播放、收藏排序，可翻页 | 否 |
| `get_up_info` | UP 主资料：签名、粉丝、关注、投稿数、获赞、等级、认证、大会员 | 否 |
| `get_feed` | 刷首页推荐流，每批 30 条 | 否 |
| `get_video_info` | 标题、作者、发布时间、简介、播放/点赞等统计 | 否 |
| `get_parts` | 多段视频（分 P）的分段清单 | 否 |
| `get_comments` | 主评论，可选热度序或时间序，游标续取，不含楼中楼 | 是 |
| `get_comment_replies` | 楼中楼，按主评论 id 获取 | 是 |
| `get_danmaku` | 弹幕，按热度取一批，按时间轴返回 | 否 |
| `get_subtitles` | 字幕，逐句，含起止秒数，可指定语种 | 是 |
| `get_frame` | 截取指定时刻一帧，返回内联图片 | 是 |
| `get_login_status` | 联网查询当前是否已登录B站 | — |
| `logout` | 退出登录 | — |
| `start_qr_login` · `complete_qr_login` | 扫码登录 | — |

`search_videos`、`list_up_videos`、`get_feed`、`get_parts`、`get_comments`、`get_comment_replies`、`get_subtitles` 接受可选的 `jq` 参数，在返回前筛选本批条目或只保留部分字段，例如字幕只要文本：`map(.content) | join("\n")`。

## 安装

需要 [uv](https://docs.astral.sh/uv/)（自带 Python ≥ 3.13）与 git；视频帧另需本机安装 ffmpeg。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 未安装 uv 时先执行
git clone https://github.com/liu-xindi/polylens-bilibili.git
cd polylens-bilibili && uv sync
```

接入 **Claude Code**：

```bash
claude mcp add polylens-bilibili -- uv run --directory /绝对路径/polylens-bilibili polylens-bilibili
```

接入 **Claude Desktop**（写入配置的 `mcpServers`）：

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

若要让 claude.ai 的网页或手机连接器接入，则部署为公网服务：Streamable HTTP 传输 + 内置 OAuth。

配置（命令行 > 环境变量 > 默认）：

| 项 | 环境变量 | 命令行 | 默认 |
|---|---|---|---|
| 传输 `stdio`/`http` | `POLYLENS_BILIBILI_TRANSPORT` | `--transport` | `stdio` |
| 监听地址 | `POLYLENS_BILIBILI_HTTP_HOST` | `--host` | `127.0.0.1` |
| 监听端口 | `POLYLENS_BILIBILI_HTTP_PORT` | `--port` | `6622` |
| 公网地址 | `POLYLENS_BILIBILI_PUBLIC_URL` | `--public-url` | 无 |
| 机主口令 | `POLYLENS_BILIBILI_AUTH_SECRET` | 仅环境变量 | 无 |
| 允许无鉴权 | `POLYLENS_BILIBILI_INSECURE_NO_AUTH` | 仅环境变量 | 否 |

公网地址与机主口令均设置后启用 OAuth；**缺其一则拒绝启动**。本机调试确需无鉴权时设 `POLYLENS_BILIBILI_INSECURE_NO_AUTH=1`，此时服务不校验任何身份，凡能连到监听端口的都可调用全部工具，包括读取已保存的登录凭据。

```bash
POLYLENS_BILIBILI_TRANSPORT=http \
POLYLENS_BILIBILI_PUBLIC_URL=https://example.com \
POLYLENS_BILIBILI_AUTH_SECRET='<口令>' \
uv run polylens-bilibili
```

在 claude.ai 添加连接器：URL 填 `https://example.com/mcp`，经 OAuth 授权后在同意页输入机主口令，仅首次需要。

## 登录与 Cookie

凭据存于 `~/.cache/polylens-bilibili/cookie`，`logout` 可删除。

## 免责声明

本工具供个人学习与研究使用，只读取内容，不向平台写入任何数据。需要登录的功能使用你自己的凭据，在你账号的权限范围内访问，不绕过付费墙或内容保护。所获内容版权归原发布方，使用者须自行遵守法律、平台条款与版权规定，并承担使用后果。本软件按现状提供，不附任何担保，详见 LICENSE。

## 许可证

以 Apache License 2.0 授权，见 [LICENSE](LICENSE)。
