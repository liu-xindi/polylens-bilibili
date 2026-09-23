# polylens-bilibili

polylens-bilibili 是一个 MCP 服务，用于读取 B 站公开视频的相关信息。

## 工具

| 工具 | 说明 | 登录 |
|---|---|---|
| `search_videos` | 按关键词搜索视频，可翻页 | 否 |
| `get_feed` | 刷首页推荐流 | 否 |
| `get_video_info` | 标题、作者、发布时间、简介、播放/点赞等统计 | 否 |
| `get_parts` | 多段视频（分 P）的分段清单 | 否 |
| `get_comments` | 主评论，可选热度序或时间序，游标续取，不含楼中楼 | 是 |
| `get_comment_replies` | 楼中楼，按主评论 id 钻取 | 是 |
| `get_danmaku` | 弹幕，按热度取一批，按时间轴返回 | 否 |
| `get_subtitles` | 字幕，逐句，含起止秒数，可指定语种 | 是 |
| `get_frame` | 截取指定时刻一帧，返回内联图片 | 是 |
| `get_login_status` | 联网核验本地凭据是否仍然有效 | — |
| `logout` | 删除本地 Cookie | — |
| `start_qr_login` · `complete_qr_login` | 扫码登录 | — |

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

公网地址与机主口令均设置后启用 OAuth；缺其一则以无鉴权运行。此时服务不校验任何身份，凡能连到监听端口的都可调用全部工具，包括读取已保存的登录凭据。

```bash
POLYLENS_BILIBILI_TRANSPORT=http \
POLYLENS_BILIBILI_PUBLIC_URL=https://example.com \
POLYLENS_BILIBILI_AUTH_SECRET='<强口令>' \
uv run polylens-bilibili
```

在 claude.ai 添加连接器：URL 填 `https://example.com/mcp`，经 OAuth 授权后在同意页输入机主口令，仅首次需要。

## 登录与 Cookie

登录方式是扫码：调 `start_qr_login`，二维码作为图片直接返回在对话里，用 B站 App 扫码并在手机上确认后调 `complete_qr_login`，凭据自动写入本地。

凭据存于 `~/.cache/polylens-bilibili/cookie`，`logout` 可删除。

## 免责声明

polylens-bilibili 是开源工具，从 B 站的公开内容链接中提取信息，供个人学习、研究与技术交流使用。

只读取内容，不修改，不上传，不向平台回写任何数据。访问基于用户自行提供的登录凭据，在用户自身账号的权限范围内进行，不绕过付费墙或平台的内容保护措施。对平台接口的调用为按其既定协议发起请求的互操作实现，不解密平台的受保护内容。数值配置随代码发版，不含远程配置。

使用中产生的行为由使用者负责，需自行遵守适用法律、平台服务条款与版权规定。提取所得内容的版权归原发布方所有，其使用、留存与再分发由使用者自行判断并承担责任。

按现状提供，不含任何明示或默示担保。使用或无法使用所引发的任何损失、纠纷或后果，由使用者自行承担。

## 许可证

以 Apache License 2.0 授权，见 [LICENSE](LICENSE)。
