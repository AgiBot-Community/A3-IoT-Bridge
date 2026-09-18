# 智元 A3 HDU 智能家居 Agent 交付说明

本项目部署在智元远征 A3 的 HDU 上，通过 LinkSoul 接收机器人语音识别文本，使用本地确定性规则和千问理解用户意图，再通过局域网 HTTP 调用小鸿 SE。小鸿 SE 收到命令后控制灯光、风扇、传感器和声光报警器，并把模组真实 ACK 返回给 A3，最终由机器人播报执行结果。

本交付包不包含任何真实账号密码、APP Secret、千问 API Key、小鸿 SE Token、机器人登录密码或现场固定 IP。部署时必须使用接收方自己的凭证。

## 1. 当前功能

- LinkSoul `Asr2Llm` 文本回调与 A3 语音播报。
- 千问 `qwen3.5-omni-flash` 自然对话与家居意图理解。
- 明确家居指令走本地确定性规则，避免大模型跳过工具或虚报执行结果。
- 小鸿 SE UDP 自动发现，不依赖 DHCP 固定 IP。
- 单灯、单风扇、灯光颜色、全屋灯光、全屋风扇控制。
- 温湿度、光照、设备状态和当前模式查询。
- 居家、离家、普通、明亮、氛围、通风和晚安模式。
- “我回家了”执行：进入居家模式，然后在收到真实 ACK 后打开客厅灯。
- 声光报警器控制、全屋状态汇报、上下文指代和撤销上一笔操作。
- A3 舞蹈动作与小鸿 SE 灯效联动；出厂配置默认关闭动作。
- ASR 热词纠错、碎片合并、重复事件与播报回声抑制。

## 2. 运行环境

参考验证环境：

- 机器人：智元远征 A3（不是 A3 Ultra）。
- AimDK：A3 v3.2 系列。
- Linux 用户：`agi`。
- Python：机器人系统自带 Python 3。
- LinkSoul AgentSDK：`1.4.0a0`，离线 wheel 已随交付包提供。
- A3、小鸿 SE 必须接入同一个局域网。
- LinkSoul 和千问需要机器人能够访问互联网。

小鸿 SE 侧需要提供：

- HTTP 服务端口，默认 `8080`。
- UDP 自动发现端口，默认 `37020`。
- Bearer Token。
- 与本项目一致的设备 ID、场景 ID 和 HTTP 接口。

## 3. LinkSoul 平台配置

1. 在智元开发者控制台创建应用并开通 AgentSDK，保存 `APP ID`、`APP Key` 和 `APP Secret`。
2. 在 LinkSoul 创建“SDK 接入”智能体，并绑定上面的应用。
3. 输入格式选择“ASR 识别结果”。
4. 输出格式选择“文本”，由 LinkSoul 调用机器人 TTS 播报。
5. 技能调用方式选择由 Agent 处理，不要同时配置另一套相同的家居回复逻辑。
6. 将 A3 设备绑定到该智能体，并确认智能体 code 与机器人绑定关系正确。

建议配置“远征远征”为唤醒词，并在热词中加入客厅、主卧、次卧、风扇、温湿度、明亮模式、氛围模式、通风模式、声光报警器等词。

## 4. 复制到机器人

推荐目录固定为：

```bash
/agibot/data/home/agi/Desktop/a3_home_bridge
```

在电脑上把本目录完整复制到 A3，例如：

```bash
scp -r 智元A3-HDU agi@<A3_IP>:/agibot/data/home/agi/Desktop/a3_home_bridge
```

如果目标目录已经存在，应先由部署人员确认哪些配置需要保留，不要直接覆盖现场的 `.env` 和 `config.json`。

## 5. 安装依赖

登录 A3 后执行：

```bash
cd /agibot/data/home/agi/Desktop/a3_home_bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

核心 HTTP、UDP、规则解析和千问客户端只使用 Python 标准库；LinkSoul SDK 使用交付包中的官方离线 wheel。其运行依赖 `websocket-client` 和 `PyJWT` 会由 `requirements.txt` 安装。

## 6. 配置非敏感参数

复制配置模板：

```bash
cp config.example.json config.json
```

默认启用 UDP 自动发现，因此 `gateway.base_url` 可以保持为空。房间与设备映射在 `config.json` 中：

| 房间 | 灯光 | 风扇 |
|---|---|---|
| 客厅 | `light_1` | `fan_1` |
| 主卧 | `light_2` | `fan_2` |
| 次卧 | `light_3` | 无 |

如果小鸿 SE 使用不同的设备 ID、发现端口或服务名，应同步修改 `config.json`。

## 7. 配置凭证

复制环境变量模板：

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，替换以下值：

```text
LINKSOUL_APP_ID
LINKSOUL_APP_KEY
LINKSOUL_APP_SECRET
XHSE_BEARER_TOKEN
```

### 配置千问 Key

推荐将千问 Key 放在项目目录外：

```bash
mkdir -p /agibot/data/home/agi/.secrets
chmod 700 /agibot/data/home/agi/.secrets
```

使用文本编辑器创建：

```text
/agibot/data/home/agi/.secrets/dashscope_api_key
```

文件内容只放一行接收方自己的千问 API Key，并执行：

```bash
chmod 600 /agibot/data/home/agi/.secrets/dashscope_api_key
```

如需换用其他千问模型，修改 `.env` 中的 `QWEN_MODEL`。当前经过验证的默认值为：

```text
qwen3.5-omni-flash
```

启动脚本也支持直接通过 `DASHSCOPE_API_KEY` 环境变量传入 Key，但不要把真实 Key 写入代码、README 或提交到仓库。

## 8. 部署前测试

先运行不依赖机器人和小鸿 SE 的自动化测试：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

当前交付版本应通过 53 项测试。

随后检查小鸿 SE：

```bash
set -a
source ./.env
set +a
python -m a3_bridge.cli --config config.json --health
```

再进行一次不会涉及机器人动作的灯光测试：

```bash
python -m a3_bridge.cli --config config.json --once "打开客厅灯"
python -m a3_bridge.cli --config config.json --once "关闭客厅灯"
```

只有返回真实成功结果后，才能认为 A3 到小鸿 SE 的链路已打通。

## 9. 前台启动

```bash
chmod +x run_linksoul_qwen.sh
./run_linksoul_qwen.sh
```

正常启动应看到：

```text
A3 智能家居桥接服务已启动
小鸿 SE 预连接成功
LinkSoul 认证成功
```

此时可以对机器人说：

```text
远征远征
打开客厅灯
```

机器人播报结束后，建议等待约 1～2 秒再唤醒下一轮，避免 LinkSoul 在 TTS 与 ASR 切换期间漏掉语音。

## 10. 安装为用户服务

确认前台运行正常后：

```bash
mkdir -p ~/.config/systemd/user
cp deployment/a3-home-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now a3-home-bridge.service
systemctl --user status a3-home-bridge.service
```

查看运行日志：

```bash
journalctl --user -u a3-home-bridge.service -f
```

更新程序后重启：

```bash
systemctl --user restart a3-home-bridge.service
```

如果部署目录不是 `/agibot/data/home/agi/Desktop/a3_home_bridge`，需要同步修改 service 文件中的 `WorkingDirectory` 和 `ExecStart`。

## 11. 舞蹈功能安全说明

交付配置中：

```text
A3_DANCE_ENABLED=false
```

因此安装、启动或语音误识别都不会触发机器人舞蹈动作。只有机器人已离开包装箱、切换到允许执行技能的状态、周围空间清空并由现场人员确认安全后，才能将该值改为 `true`。

启用后，“开始跳舞”会协调 A3 内置技能和小鸿 SE 灯效，持续约 18 秒；“停止跳舞”具有优先级。

## 12. 数据链路

```text
用户语音
  → A3 麦克风与 LinkSoul ASR
  → HDU 自定义 Agent
  → 本地规则或千问意图理解
  → 小鸿 SE UDP 自动发现
  → 局域网 HTTP 控制
  → 星闪模组真实 ACK
  → HDU 解析执行结果
  → LinkSoul TTS 播报
```

程序不会仅凭 HTTP `200` 判断成功，还会检查 `code`、`execution_state`、设备在线状态和场景完成数量。网络重试使用相同的 `request_id`，避免重复执行。

## 13. 主要目录

```text
a3_bridge/                 主程序
  linksoul_app.py          LinkSoul SDK入口
  qwen_home_agent.py       千问对话与工具调用
  bridge.py                家居安全执行层
  intent_parser.py         本地意图识别与路由
  xiaohong_client.py       小鸿 SE HTTP客户端
  discovery.py             UDP自动发现
  dance_controller.py      舞蹈与灯效协调
deployment/                systemd用户服务
tests/                     自动化测试
config.example.json        非敏感配置模板
.env.example               凭证变量模板
run_linksoul_qwen.sh       正式启动脚本
```

交付包还保留了早期本地 Vosk、ROS 音频探针和旁路调试代码，便于排障和方案对比；它们不属于默认 LinkSoul 服务，且离线 Vosk 模型未包含在本交付包中。正式部署只需使用 `run_linksoul_qwen.sh`。

## 14. 交付前安全检查

上传代码仓库前确认不存在以下文件：

```text
.env
config.json
secret.txt
千问.txt
任何真实 API Key、APP Secret、Bearer Token
SSH 密码和私钥
state/desired_mode.json
```

`.gitignore` 已覆盖常见敏感文件，但仍应在提交前执行人工检查。若真实凭证曾经进入 Git 历史，应立即在对应平台吊销并重新生成，单纯删除文件不够。

## 15. 常用验收口令

```text
你可以做什么
打开客厅灯
关闭主卧风扇
把次卧灯调成蓝色
温湿度是多少
打开明亮模式
关闭全部灯光
进入离家模式
我回家了
屋内有紧急情况，立即报警
解除报警
我要睡觉了
家里现在怎么样
```

“我回家了”应切换为居家模式并打开客厅灯；只有模式和客厅灯两次 ACK 都成功后，机器人才能回复执行完成。
