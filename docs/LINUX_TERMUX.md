# Linux / Android Termux 与局域网工位

本流程适用于 Python 3.11+ 的 Linux 和 64 位 Android Termux。Control 的服务、Agent CLI 和 MCP 使用 Python 标准库；手机接入已有办公室不需要 Electron、图形桌面、Node.js 或 systemd。Codex 本身的安装、登录和模型访问由手机已有环境负责。

典型连接为：手机 Agent → 手机 `127.0.0.1:17921` 网关 → 局域网 Link → 电脑 `127.0.0.1:17910` Control。办公室、群聊、任务和项目记忆仍保存在电脑；项目工作目录由手机自行准备。Link 不复制文件、不启动模型，也不自动转移第三方聊天。

## 1. 手机获取程序

在 Termux 的私有 HOME 中执行，不要在 Android 公共下载目录或共享存储中安装：

```sh
pkg update
pkg install python git golang
git clone https://github.com/jiangshanyao2200-hue/Aieyra-Control.git "$HOME/Aieyra-Control"
cd "$HOME/Aieyra-Control"
python scripts/control.py doctor
python scripts/control.py install-link
python scripts/control.py link version
```

普通 Linux 使用发行版的 Python 3.11+、Git 和 Go 1.26.8+，命令中的 `python` 可换成 `python3`。`install-link` 从 Aieyra OS 公开仓库的固定提交 `ed6201cfe44cbfbfb51915e38d37bbfdea410248` 构建 Link 0.3.0，复用原实现，不维护第二套传输协议。构建输出放在 `runtime/link`，安装器先验证原生程序版本，再以不覆盖方式发布；已存在时停止，更新应先审阅并选择新的 `--output`。

安装器自动区分 Android 与 Linux、ARM64 与 x64。Termux 使用 `pkg` 提供的 Go，设置 `GOTOOLCHAIN=local`；如 Go 低于 1.26.8，先更新 Termux 软件包。构建需要访问 GitHub 与 Go 模块源。普通 Linux 二进制不能直接当成 Android 版本。已有可信原生 Link 可用绝对路径 `AIEYRA_LINK_BINARY` 指定，跳过构建。

`doctor` 不连接办公室，不创建数据或工位。`link.status=not_installed` 表示本地 Python 功能可用但尚不能通过 Link 跨设备连接。

## 2. 电脑提供现有办公室

保持原 Control 运行，只启动一次。以下命令在电脑 Control 源码目录执行；使用完整 Windows 包时也可从“连接”窗口完成相同配对和接入。

先确认 `python scripts/control.py link version` 能运行。缺少 Link 时可用 `install-link` 构建，或用 `AIEYRA_LINK_BINARY` 指向已核验的原生版本。

电脑终端 A：

```text
python scripts/control.py link host --data PRIVATE_LINK_DIRECTORY --hub-url https://DESKTOP-LAN-HOST:17920 --listen 0.0.0.0:17920
```

`PRIVATE_LINK_DIRECTORY` 必须替换为源码之外的绝对私有目录；`DESKTOP-LAN-HOST` 替换为手机可达的电脑局域网地址或主机名。保持终端运行，只允许所需局域网访问 TCP 17920。不要把 Control 的 17910 端口改为公网或局域网监听。

电脑终端 B 使用相同私有目录：

```text
python scripts/control.py link control-attach --data PRIVATE_LINK_DIRECTORY --control-url http://127.0.0.1:17910 --name MY-OFFICE
```

保持此终端运行。它使用该 Link 主机的 owner 身份接入现有办公室。默认仅代理经过原凭据鉴权的 Agent API；确需共享办公室画面及聊天历史时才增加 `--share-view`，范围见 [Link 指南](LINK.md)。

终端 A 首次启动输出十分钟有效的一次性 `device_code`，私密传递到手机。若需要新码，在电脑另一终端执行：

```text
python scripts/control.py link code --data PRIVATE_LINK_DIRECTORY
```

重启时保留原私有目录和主机地址；已有 Link 服务器可直接复用，无需再建一台。

## 3. 手机配对并开网关

手机 Termux 终端 A，使用 Bash 的隐藏输入，避免设备码进入命令历史：

```sh
cd "$HOME/Aieyra-Control"
read -r -s -p 'Link device code: ' AIEYRA_LINK_CODE
printf '\n'
export AIEYRA_LINK_CODE
python scripts/control.py link connect --name MY-PHONE
unset AIEYRA_LINK_CODE
python scripts/control.py link control-status
```

从 `control-status` 的 `services` 中选择自己的办公室，复制其 `id`（形如 `DEVICE_ID:control`），再运行：

```sh
python scripts/control.py link control-connect --service DEVICE_ID:control --listen 127.0.0.1:17921
```

保持该终端运行。手机 Agent 只连接本机 17921，Link 负责固定 TLS 指纹的配对及端到端加密。设备配对成功不等于取得 Agent 身份或已经加入工位。默认 Link 私有目录为 `$HOME/.config/AieyraLink`；如设置了 `XDG_CONFIG_HOME` 则遵循该变量。需要自定义时每条命令使用相同 `--data`。

## 4. 迁移获准使用的工位档案

同一项目使用原工位。若电脑会话仍在工作，先协调工作范围和真正交接时间，不能从手机抢占；也不能通过新建重复工位绕过占用。导出工具会拒绝活动中的工位。

电脑原 owner 先保存五段记忆并 `finish` 自己的真实原生会话，确认释放成功，再在电脑执行：

```text
python scripts/station-transfer.py export --profile ORIGINAL_PRIVATE_STATION_FILE --output NEW_PRIVATE_TRANSFER_FILE
```

这份 JSON **含原 Agent 凭据**，只允许导出本人或已获准移交的身份。通过可信私密传输送到手机 `$HOME/.config/aieyra-control-transfer.json`；不要提交 Git、发到群聊或公开仓库。工具不会复制原路径、native ID、通信租约、项目正文或聊天，也不会撤销原凭据。

手机终端 B：将下方工作目录替换为手机实际的项目仓库；它必须已经存在，可以是同一项目的手机 checkout。不要用电脑的盘符路径。

```sh
cd "$HOME/Aieyra-Control"
umask 077
chmod 600 "$HOME/.config/aieyra-control-transfer.json"
python scripts/station-transfer.py import \
  --bundle "$HOME/.config/aieyra-control-transfer.json" \
  --profile "$HOME/.config/aieyra-control/stations/project.json" \
  --root "$HOME/projects/YOUR-PROJECT" \
  --host codex --port 17921
```

导入会通过正在运行的本机网关验证原 actor、seat、项目和凭据，保存手机本地路径；目标已有不同配置时拒绝覆盖。中断后只用相同参数重试。`joined:false` 和 `handoff_performed:false` 是正常结果：导入不接管工位。

手机当前 Agent 用自己的**真实 native session ID** 执行：

```sh
python scripts/agent-station.py --profile "$HOME/.config/aieyra-control/stations/project.json" doctor
python scripts/agent-station.py --profile "$HOME/.config/aieyra-control/stations/project.json" join --native-session-id REAL_PHONE_NATIVE_SESSION_ID --resume
```

若返回 `handoff_required`，由现有获授权领导按原 `station/handoff` 协议执行显式 CAS：使用手机真实 native ID、原 actor、当前 binding version 和明确原因，且原通信连接须已释放。查询原回执后再运行手机 `join`。导入工具及 Link 都不替你换绑；不能借用电脑 native ID。

成功的 `join` 才表示手机已接入原办公室。让手机 Agent 继续读项目 `AGENTS.md`、五段记忆、运行收件箱及协调消息。已有 Codex 适配可用本程序的 `agent-station.py ... install` 和 `... codex`，完整合同见 [Agent 适配器](AGENT_ADAPTERS.md)；不需要向 Control 上传 Codex 登录资料。确认导入与备份完成后，删除传输文件的多余副本，保留手机档案、相邻 `.state` 和原私有数据。

结束工作前保存五段记忆，再执行：

```sh
python scripts/agent-station.py --profile "$HOME/.config/aieyra-control/stations/project.json" finish --native-session-id REAL_PHONE_NATIVE_SESSION_ID
```

`finish` 不代写记忆，也不把任务自动标为完成。

## 可选：Linux 或手机独立运行本地 Control

连接电脑办公室只需 Link 网关和 Agent CLI；需要独立本地办公室时才启动：

```sh
python scripts/control.py run
# 或明确选择源码外的数据根：
python scripts/control.py run --data-root "$HOME/.local/share/aieyra-control-local"
```

浏览器访问 `http://127.0.0.1:17910`。前台启动，Ctrl-C 或 SIGTERM 会调用现有清理流程并释放数据锁；无后台守护或自动启动模型。Linux 默认数据目录为 `~/.local/share/Aieyra Control/data`，遵循 README 中已有的环境变量与定位文件。新文件采用仅当前用户可读写权限。配置中项目登记仍需明确授权。

本次范围为本地服务与局域网 Agent 协作。Linux/Termux 的官方账号凭据持久化尚无对应系统密钥库实现；不要据此宣称完整桌面或云账号流程已经适配。

## 断线与验证边界

- 手机 Wi-Fi、电脑防火墙和无线接入点须允许设备互通；访客 Wi-Fi 的客户端隔离可能阻断连接。普通 USB 线不自动提供网络。
- Android 可能停止后台进程。保持 Termux 会话活跃，必要时按手机系统管理电池限制；若已有 Termux 的 `termux-wake-lock`，只在本次工作需要时启用并在结束后释放。不要通过伪造活动保持工位永久在线。
- 网关退出后保留所有身份与 `.state`；恢复相同 Link 网关，再由实际工作边界执行原档案 `join --resume`。未知写入只查询原 request_id，不生成新请求重放。
- `private_file_requires_mode_600`：检查传输文件位于 Termux 私有 HOME，执行 `chmod 600`；共享存储不具备所需权限和硬链接语义。
- `go_failed_check_installed_toolchain_and_network`：核对 Go 版本、Termux 软件包和网络；不要下载 Linux x64 程序冒充 Android ARM64。
- 2026-10-02 已在 Ubuntu 24.04 / Python 3.12 验证前台启动、SIGTERM 清理、私有权限、档案导入、真实 TLS Link 网关的加入与退出，以及新 native 必须交接。Link Linux ARM64、Android ARM64 已交叉构建；当前没有 Android 真机运行回执，不把交叉构建称为真机验收。

开发者可设置 `AIEYRA_LINK_TEST_BINARY` 为原生 Link 的绝对路径，运行 `python -m unittest discover -s tests/service -p test_linux_termux.py -v`，再运行原 `test_device_link.py` 验证记忆、通知、交接、重启和撤销。
