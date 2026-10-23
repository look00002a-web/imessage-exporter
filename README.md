# iMessage 导出器（macOS）

把 Mac 本机「信息（Messages）」里的 **iMessage / 短信（SMS）** 聊天记录，
本地、离线、只读地导出成好读、好留存的格式：

- **自包含单文件 HTML**（iMessage 气泡样式、内嵌图片，双击即可在浏览器打开/打印成 PDF）
- **纯文本 TXT**（按时间排版，适合检索、归档）
- **表格 CSV**（带 UTF-8 BOM，Excel / 数字 直接打开）

可以挑选某个会话、限定时间范围再导出，也可以一次导出全部会话。

> 本工具仅用于导出、备份 **你自己设备上、属于你本人** 的聊天记录。请勿用于侵犯他人隐私。

---

## ⭐ 最简单的用法：双击运行（不用记任何命令）

1. **先给终端「完全磁盘访问权限」**（只需一次，步骤见下文「第一步」）。
2. 双击本文件夹里的 **`双击这里导出.command`**。
3. 它会在终端里一步步问你：
   - 列出你的会话，让你**输入编号**选择（回车=全部）；
   - 问你要**什么格式**（回车=网页 HTML）；
   - 问你要不要**限定时间段**（回车=全部时间）。
4. 导出好的文件会自动保存到**桌面的「iMessage导出」文件夹**，并自动帮你打开它。

> 第一次双击如果提示没权限，它会**自动帮你打开**那个设置面板，按它说的把终端打开开关、
> 完全退出终端重开，再双击一次就行。
>
> （如果双击时系统提示「无法打开，因为来自身份不明的开发者」：在该文件上**右键 › 打开**，
> 再点一次「打开」即可；或在「系统设置 › 隐私与安全性」里点「仍要打开」。）

下面是给想用命令行的人看的详细说明。

---

## 为什么它很简单（和微信不同）

iMessage 的数据库 `~/Library/Messages/chat.db` 是 **明文 SQLite**，
不加密、不需要提取密钥、不需要关闭 SIP、不需要 Frida。
唯一的前提是：**运行本工具的终端需要「完全磁盘访问权限」**（下面有详细步骤）。

全程：

- ✅ **只读**：读取前先把数据库连同 `-wal` / `-shm` 复制到临时目录，再以只读方式打开，
  **绝不碰、绝不回写**系统里的原始数据库。
- ✅ **离线**：不联网。只用 Python 3 标准库。
- ✅ **零额外依赖**：无需 `pip install` 任何东西。
  （可选：导出 HEIC 图片时会调用 macOS 自带的 `sips` 命令转码，这也是本机命令、不联网。）

---

## 环境要求

- macOS（自带 `python3` 即可，无需额外安装）
- 终端拥有「完全磁盘访问权限」

验证一下 Python：

```bash
python3 --version
```

显示 `Python 3.x.x` 即可（系统自带的 3.9 也完全够用）。

---

## 第一步（关键）：给终端「完全磁盘访问权限」

`~/Library/Messages/` 是受系统保护的目录，默认任何程序都读不到。
需要手动授权一次：

1. 打开 **系统设置**（System Settings）
2. 进入 **隐私与安全性**（Privacy & Security）› **完全磁盘访问权限**（Full Disk Access）
3. 点 **＋**，把你用来运行本工具的 **终端** 加进去：
   - 用「终端」App 就加 **Terminal**（路径：`/System/Applications/Utilities/Terminal.app`）
   - 用 iTerm2 就加 **iTerm**
   - 在 VS Code 的集成终端里跑，就加 **Visual Studio Code**
4. 确认它的开关是 **打开** 状态
5. **完全退出并重开该终端**（⌘Q 退出，再重新打开），授权才会生效

> 没授权时运行会看到类似「没有权限读取聊天数据库」的提示，照上面做一遍即可。

---

## 用法

把 `imessage_export.py` 放到任意目录，在终端 `cd` 进去运行即可。

### 1. 先列出所有会话（带编号）

```bash
python3 imessage_export.py list
```

输出示例：

```
   #  类型       消息数  最后活动              会话
------------------------------------------------------------------------------
   1  单聊          12  2026-10-21 14:37  Apple
   2  单聊         231  2026-10-19 10:20  10086
   3  单聊         177  2026-10-18 09:20  +8613800138000
   ...
```

左边的 **#** 就是导出时用的编号。

列出联系人标识（手机号 / 邮箱）而不是会话：

```bash
python3 imessage_export.py list --contacts
```

### 2. 导出某个会话

```bash
# 导出第 3 个会话为 HTML（默认格式，默认输出到 ./imessage-export/）
python3 imessage_export.py export --chat 3

# 同时导出 HTML + TXT + CSV
python3 imessage_export.py export --chat 3 --format html,txt,csv

# 按手机号/邮箱导出（单聊）
python3 imessage_export.py export --handle +8613800138000

# 限定时间范围（本地时间，含首尾两天）
python3 imessage_export.py export --chat 3 --start 2024-01-01 --end 2024-12-31

# 指定输出目录
python3 imessage_export.py export --chat 3 --output ~/Desktop/我的聊天备份
```

### 3. 一次导出全部会话

```bash
python3 imessage_export.py export --all --format html,txt
```

导出完在输出目录里，每个会话对应一组文件：
`<编号>_<会话名>.html` / `.txt` / `.csv`。

---

## 选项速查

| 选项 | 说明 |
| --- | --- |
| `--chat N` | 按 `list` 里的编号选择会话 |
| `--handle 号码/邮箱` | 按手机号或邮箱选择单聊 |
| `--chat-id ROWID` | 按数据库 `chat` 表的 ROWID 选择 |
| `--identifier xxx` | 按 `chat_identifier` 选择 |
| `--all` | 导出全部会话 |
| `--start YYYY-MM-DD` | 起始日期（含当天，本地时间） |
| `--end YYYY-MM-DD` | 结束日期（含当天，本地时间） |
| `--format html,txt,csv` | 导出格式，逗号分隔，默认 `html` |
| `--output DIR` / `-o DIR` | 输出目录，默认 `./imessage-export` |
| `--attachments embed\|copy\|none` | 附件处理方式，默认 `embed` |
| `--max-embed-mb N` | 单张内嵌图片大小上限（MB，默认 10） |
| `--image-max-px PX` | 内嵌图片最长边缩放到该像素（需 `sips`，0=原尺寸） |
| `--names file.json` | 联系人名字映射，见下文 |
| `--db PATH` | 指定 chat.db 路径（默认系统路径） |

查看完整帮助：

```bash
python3 imessage_export.py --help
python3 imessage_export.py export --help
```

---

## 附件（图片 / 视频 / 文件）怎么处理

三种模式，用 `--attachments` 选择：

- **`embed`（默认）**：生成 **自包含的单个 HTML 文件**——
  把**图片**直接以 base64 内嵌进去（HEIC 会用 `sips` 自动转成 JPEG 以便浏览器显示）。
  视频 / 音频 / 其它文件因为体积大，只显示为一张说明卡片（不内嵌）。
  适合：普通会话，一个文件就能随手转发 / 打印。

- **`copy`**：导出 HTML + 一个同名的 `_附件` 文件夹，把**所有附件的原件**
  （图片、视频、文件都在内）复制出来，HTML 里用相对路径引用。
  适合：想**完整保留真实媒体**（尤其是视频）的归档。
  注意：此模式 HEIC 保留原始文件；浏览器里不一定能预览 HEIC，可用「预览 / 照片」App 打开。

- **`none`**：完全不导出附件内容，只在正文位置留一张文件名卡片。

**照片很多、单个 HTML 太大？** 加上 `--image-max-px 1600`（或 1200），
会用 `sips` 把内嵌图片等比缩小到最长边 1600 像素，文件体积能降一个数量级，
原始数据库和原始照片都不受影响。或者改用 `--attachments copy`。

---

## 给号码起个名字（可选）

chat.db 里只有手机号 / 邮箱，没有通讯录里的姓名。
可以自己写一个 JSON 映射，让导出结果显示真实姓名：

`names.json`：

```json
{
  "+8613800138000": "张三",
  "friend@example.com": "李四"
}
```

然后：

```bash
python3 imessage_export.py export --chat 3 --names names.json
```

（你自己发出的消息始终显示为「我」。）

---

## 常见问题

**Q：提示「没有权限读取聊天数据库」。**
A：按上面「第一步」把你的终端加入「完全磁盘访问权限」，并 **完全退出重开终端**。

**Q：导出的 HTML 里图片不显示 / 是个破图。**
A：多半是 HEIC 格式且系统没有 `sips`（极少见），或用了 `copy` 模式在非 Safari 浏览器里看 HEIC。
用默认的 `embed` 模式会自动把 HEIC 转成 JPEG；或用 Safari 打开；或用「照片 / 预览」打开原件。

**Q：有些消息正文是空的 / 只有「附件」。**
A：那条消息本身可能就只有图片等附件而没有文字；本工具已尽力从 `attributedBody`
里提取正文（新版 macOS 的文字正文常存在这里，本工具实测可全部提取）。

**Q：会不会改到我的聊天记录？会不会上传到网上？**
A：都不会。工具先把数据库复制到临时目录再**只读**打开，不回写原库；全程不联网。

**Q：群聊能导出吗？**
A：能。群聊会显示每条消息的发送者；单聊则只区分「我」和对方。

**Q：导出的时间对不上？**
A：数据库里存的是「2001-01-01 以来的纳秒」（Mac 纪元），本工具已换算成你电脑的**本地时间**。

---

## 技术说明（给好奇的人）

- 数据库：`~/Library/Messages/chat.db`（明文 SQLite）。
- 主要表：`message` / `handle` / `chat` / `chat_message_join` /
  `chat_handle_join` / `attachment` / `message_attachment_join`。
- 正文：优先取 `message.text`；为空时解析 `message.attributedBody`
  （`NSKeyedArchiver` / typedstream 二进制，新版 macOS 常把正文放这里）。
- 时间：`message.date` 是 Mac 纪元纳秒，换算公式
  `unix = date/1e9 + 978307200`（旧系统里 `date` 为秒，本工具自动判别）。
- 方向：`is_from_me`；对端：`handle.id`；群聊：`chat.display_name` / `chat.chat_identifier`。
- 回应（tapback，爱心/赞/大笑等）会归到被回应的那条消息下显示。
- 附件：`attachment.filename`（`~` 会自动展开为用户目录）。

---

## 许可

个人自用工具，随意取用。请仅用于导出你本人的数据。
