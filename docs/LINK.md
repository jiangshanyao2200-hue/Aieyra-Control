# 用 Link 连接设备与工位

Linux / Android Termux 的无桌面启动、原生 Link 源码构建及手机工位档案导入，见 [Linux / Termux 指南](LINUX_TERMUX.md)。

Control 0.7 的“连接”入口使用 Aieyra Link 0.3。Link 部署在你自己的电脑或私人服务器上，通过设备码把设备接到同一中继。它不要求官方云服务或账号；本地办公室继续保存在本地。Windows 完整发行包内置 Link，源码安装可将经核验的可执行文件放到 `runtime/link/aieyra-link.exe`（Linux/macOS 为 `aieyra-link`），或用绝对路径环境变量 `AIEYRA_LINK_BINARY` 指定。

## 部署到私人服务器

从 Aieyra OS 应用目录取得 Link 的签名安装包，核对签名与包的 SHA-256，解压并按附带 README 安装。Linux x64 包提供 `install.sh` 和可选的 `aieyra-link.service` 模板。安装器不会自动启动服务。

在服务器执行：

```sh
aieyra-link host --data "$HOME/.config/AieyraLink" --hub-url https://YOUR-SERVER:17920 --listen 0.0.0.0:17920
```

将 `YOUR-SERVER` 替换为所有设备能访问的域名或地址，允许目标网络访问 TCP 17920。启动回执包含一次性 `device_code`，有效期十分钟。设备码包含服务器地址、固定 TLS 公钥指纹和邀请信息，是完整连接字符串，不依赖官方目录解析短号码。私密传递设备码；不要粘贴到公开帖子、截图或日志。

为下一台设备生成独立设备码：

```sh
aieyra-link code --data "$HOME/.config/AieyraLink"
```

服务器重启时保留原数据目录和地址。不要通过删除身份文件修复连接。使用 systemd 时按 Link 包附带模板创建专用用户和私有状态目录，替换服务器地址后明确启用服务，再以该用户运行 `code` 获取邀请。

## 在 Control 中连接

1. 打开侧栏“连接”，填写本设备名称和设备码。每台设备使用独立设备码，加入同一台 Link 服务器。
2. 在拥有办公室的设备上选择“接入本地工位”。默认只开放原 Agent API。需要从其他设备查看工位、群聊和任务时，主动勾选“共享本地只读办公室”后再接入。共享范围包括办公室快照、登记信息和聊天历史，请只邀请可信设备。
3. 在另一台设备刷新办公室列表并选择“连接办公室”。视图切换到远端办公室；“查看本地办公室”切回本地。切换视图不迁移任务、文件或第三方原生聊天。
4. Agent 使用返回的本机网关地址连接远端 Control，并使用远端办公室原有的本人档案、凭据与项目权限。配对设备不会创建 Agent 身份、授予领导权限或绕过原生会话交接。

配对中断时保留原连接与设备名称，用相同身份重试。Control 会恢复自身配置的桥接与网关进程；断开按钮只停止自身管理的连接，私有身份保留。每个 Control 最多保存八个连接，每个连接最多八个办公室网关。

命令行也可完成相同操作；设备码通过 `AIEYRA_LINK_CODE` 环境变量传递，配对后清除它：

```text
aieyra-link connect --name MY-DEVICE
aieyra-link control-attach --control-url http://127.0.0.1:17910 --name MY-OFFICE
aieyra-link control-status
aieyra-link control-connect --service DEVICE_ID:control --listen 127.0.0.1:17921
```

只在需要只读办公室共享时向 `control-attach` 增加 `--share-view`。所有命令均可用 `--data` 指定私有 Link 目录。Control 管理的 Link 身份位于其外部数据目录的 `link` 下，不属于软件更新内容。

## 局域网、Wi-Fi 与 USB

- 局域网和 Wi-Fi 使用主机在该网络中可达的地址。路由、防火墙和 Wi-Fi 客户端隔离须允许连接。
- USB 通过网络共享、USB 网卡或已配置的端口隧道提供 IP 路由。仅插入普通 USB 数据线不会自动形成网络。
- 不同地点的设备可以连接你部署在私人服务器上的 Link。所有设备须能访问设备码中的地址；Link 不修改路由器、不自动穿透 NAT，也不绕过防火墙。

以上方式复用同一套 TLS 配对和设备协议。当前独立包提供 Windows x64 与 Linux x64；其他平台须使用经验证的对应原生构建。当前没有独立手机界面。

## 身份、隐私和未知结果

Control 请求与回执在配对设备之间端到端加密。中继可见路由元数据和密文，不能读取办公室正文或 Control bearer token。配对时固定服务器 TLS 公钥，设备身份和签名另外核验。你仍须保护终端身份文件、原 Agent 凭据和服务器备份。

Agent 的项目权限、任务租约、五段记忆版本、显式 CAS 交接、通知已读／已处理与 finish 规则保持有效。通信在线、请求收到、实际执行和任务完成分别记录。Link 不启动模型，也不会因重连重放未知写入。

单次请求有三十秒截止时间，密文回执和恢复记录保留十分钟，队列有容量限制。Link 是实时传输，离线期间不替代 Control 的持久任务账本。写入超时或回执丢失时，先查询原 Control request_id 和回执，确认是否受理后再处理；不要使用新请求 ID 重发同一操作。

服务器所有者可用 `aieyra-link revoke --id DEVICE_ID` 撤销设备。停止只读共享或网关时只关闭本人进程。完整签名更新继续使用 B/L/N 审阅；已有源码安装要获得内置 Link，可在保留外部私有数据后使用完整发行包，或单独安装经核验的 Link。
