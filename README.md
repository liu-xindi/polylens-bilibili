# polylens-bilibili

> 从 B 站视频中提取信息，供 AI 调用。

一个 MCP server：围绕一条视频链接，从元信息、评论、楼中楼、弹幕、字幕、视频帧等维度提取内容，也可按关键词搜索视频。

面向个人学习、研究与内容辅助阅读，按需提取单条内容，非数据采集，非批量，非商业用途。

## 工具

| 工具 | 说明 | 登录 |
|---|---|---|
| `search_videos` | 按关键词搜索视频，可翻页，每条附链接 | 否 |
| `get_video_info` | 标题、作者、发布时间、简介、播放/点赞等统计 | 否 |
| `get_parts` | 多段视频（分 P）的分段清单 | 否 |
| `get_comments` | 主评论，可选热度序或时间序，游标续取，不含楼中楼 | 是 |
| `get_comment_replies` | 楼中楼，按主评论 id 钻取 | 是 |
| `get_danmaku` | 弹幕，按热度取一批，按时间轴返回 | 否 |
| `get_subtitles` | 字幕，逐句，含起止秒数，可指定语种 | 是 |
| `get_frame` | 截取指定时刻一帧，返回内联图片 | 是 |
| `get_login_status` | 联网核验本地凭据是否仍然有效 | — |
| `set_cookie` | 写入 Cookie，即时生效 | — |
| `logout` | 删除本地 Cookie | — |
| `start_qr_login` · `check_qr_login` | 扫码登录 | — |

内容类工具的 `url` 参数接受视频链接、b23.tv 短链、裸 BV/av 号，以及含链接的分享文案。

多段视频（分 P）用 `page` 参数选段，1 起。不传时取链接里的 `?p=N`，两者都没有则第 1 段。单段视频忽略这个参数。分段清单见 `get_parts`。

评论返回里除正文外还带置顶标记、UP 主点赞标记、配图地址，以及正文里链接对应的标题。

字幕的 `lang` 不传时取平台给的第一条，而同一稿件各段的轨道构成可能不同，要跨段拿同一语种就显式指定；可选值见返回的 `available_langs`。

评论的 `mode` 有 `hot`（默认）与 `newest` 两种。`hot` 是平台的综合排序，不保证按点赞降序，中断后无法从原处续取；要完整抓取或断点续取用 `newest`。

短链需要登录才能展开，未登录时改用完整视频链接。

## 安装

需要 [uv](https://docs.astral.sh/uv/)（自带 Python ≥ 3.13）与 git；视频帧另需本机安装 ffmpeg。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 未安装 uv 时先执行
git clone https://github.com/liuclare/polylens-bilibili.git
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

本地模式为 stdio，由客户端拉起进程，无需鉴权。若要让 claude.ai 的网页或手机连接器接入，则部署为公网服务：Streamable HTTP 传输 + 内置 OAuth。每实例单用户，自部署。

配置（命令行 > 环境变量 > 默认）：

| 项 | 环境变量 | 命令行 | 默认 |
|---|---|---|---|
| 传输 `stdio`/`http` | `POLYLENS_BILIBILI_TRANSPORT` | `--transport` | `stdio` |
| 监听地址 | `POLYLENS_BILIBILI_HTTP_HOST` | `--host` | `127.0.0.1` |
| 监听端口 | `POLYLENS_BILIBILI_HTTP_PORT` | `--port` | `6622` |
| 公网地址 | `POLYLENS_BILIBILI_PUBLIC_URL` | `--public-url` | 无 |
| 机主口令 | `POLYLENS_BILIBILI_AUTH_SECRET` | 仅环境变量 | 无 |

公网地址与机主口令均设置后启用 OAuth；缺其一则以无鉴权运行。此时服务不校验任何身份，凡能连到监听端口的都可调用全部工具，包括读取已保存的登录凭据。监听地址默认为本机回环；改成其他地址会让无鉴权的服务对该网络可达，运行时不阻止这种配置。

```bash
POLYLENS_BILIBILI_TRANSPORT=http \
POLYLENS_BILIBILI_PUBLIC_URL=https://example.com \
POLYLENS_BILIBILI_AUTH_SECRET='<强口令>' \
uv run polylens-bilibili
```

服务以明文 HTTP 监听，不处理 TLS。

在 claude.ai 添加连接器：URL 填 `https://example.com/mcp`，经 OAuth 授权后在同意页输入机主口令，仅首次需要。

## 登录与 Cookie

评论、楼中楼、字幕、视频帧需要登录。未登录时平台不报错，而是给出残缺却看似正常的结果，所以这几个能力会先核验登录态，未登录时直接报错。

两种登录方式：

- **手动 Cookie**：把从浏览器复制的整段 Cookie（单行）传给 `set_cookie`，即时生效，无需重启。远程或移动端也可在对话中设置。
- **扫码**：调 `start_qr_login`，二维码作为图片直接返回在对话里，用 B站 App 扫码并在手机上确认后调 `check_qr_login`，凭据自动写入本地。

凭据存于 `~/.cache/polylens-bilibili/cookie`，`logout` 可删除。

## 免责声明

本项目为开源工具，从 B 站的公开内容链接中提取信息，供个人学习、研究与技术交流使用。

本项目只读取内容，不修改，不上传，不向平台回写任何数据。访问基于用户自行提供的登录凭据，在用户自身账号的权限范围内进行，不绕过付费墙或平台的内容保护措施。对平台接口的调用为按其既定协议发起请求的互操作实现，不解密平台的受保护内容。数值配置随代码发版，不含远程配置。

使用本项目产生的行为由使用者负责，需自行遵守适用法律、平台服务条款与版权规定。提取所得内容的版权归原发布方所有，其使用、留存与再分发由使用者自行判断并承担责任。

本软件按现状提供，不含任何明示或默示担保。作者不对使用或无法使用本项目所引发的任何损失、纠纷或后果承担责任。

## 许可证

本项目以 Apache License 2.0 授权，见 [LICENSE](LICENSE)。
