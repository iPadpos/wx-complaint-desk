# 微信支付客诉工作台

Python 3.12 + Django 5.2 + MySQL 8.0，支持商户自行调用和微信渠道代调用的消费者投诉 2.0 系统。

顶部为投诉处理、商户号管理、操作记录、账号与权限；左侧每个商户独立菜单；主页面为普通投诉列表。点击投诉打开左右两栏弹窗：左侧集中显示投诉、客户和订单信息及处理按钮，右侧为微信式会话，客户白色气泡靠左、商户绿色气泡靠右，状态变化居中。两栏各自滚动，聊天标题与底部回复框固定；回复、退款审批、即时服务和反馈完成均留在弹窗内，关闭后保留列表筛选、页码和滚动位置。手机上使用全屏弹窗，投诉信息可展开或收起，默认收起以保留聊天空间。

界面使用白灰底和蓝色操作按钮。顶部导航固定，左侧商户菜单和右侧工作区各自滚动；手机上商户菜单独立横向滚动，投诉工作区纵向滚动。核验滚动时，在长菜单与长列表分别使用鼠标滚轮，确认另一分区和顶部不移动；右侧滚动后打开、关闭或按Escape退出投诉弹窗，位置应保持。

界面只保留业务数据、字段、按钮及必要的短确认、状态和错误；功能介绍、步骤教学和重复技术说明已清理。账号与权限继续使用原生Django后台，仅精简帮助文字，保留密码要求和禁用密码的影响提示。

## 已实现

- 商户新增、查询、修改、删除；保留有业务记录的商户，可关闭其自动同步。已保存商户号不能改写，以保持投诉归属。
- 渠道账号统一维护微信调用凭据；商户选择渠道账号并填写微信子商户号，可选记录支付机构商户编号。原有商户默认自行调用，保留原配置。
- 投诉按商户隔离；单号/商户订单号、状态、问题类型、用户新留言及日期筛选，分页、关联支付订单及服务单、投诉资料、客户联系方式和完整协商历史。
- 查询列表、详情和协商历史，分页拉取；日期范围自动拆为每批最多30天。工作进程每10分钟补拉最近2天。
- 文字及最多4张 JPG/PNG/BMP 图片回复，每张不超过2MB；可选 HTTPS 链接和小程序跳转，相关灰度能力需微信开通。链接必须匹配商户允许域名。
- 反馈处理完成；待处理须先回复，退款申请使用退款审批。操作前回查最新微信状态。
- 同意/拒绝退款审批反馈、预计工作日、拒绝理由、图片举证及备注。**同意审批不执行资金退款**，需通过已授权支付渠道另行退款，最终状态以微信回查为准。
- 即时服务回复：人工文本及 NormalMessage JSON，支持 TEXT、IMAGE、LINK、FAQ_LIST、BUTTON、BUTTON_GROUP。灰度与具体字段规则以商户权限和微信接口校验为准。
- 通知地址查询、创建、更新、删除，写入后回查。公网回调按商户分路由，验签、AES-GCM解密、通知去重及数据库持久任务原子保存后返回204。
- 原生 Django 登录、账号、分组及权限；CSRF；APIv3密钥、投诉详情/协商记录/回调敏感内容使用独立 DATA_KEY 加密落库；私钥文件只在服务端读取。
- RSA请求签名、响应及回调验签、手机号OAEP解密，支持微信支付公钥、平台证书及备用验签文件轮换。
- 微信写操作本地幂等及结果审计；超时、5xx或成功应答验签失败记为结果未知，阻止重复发送。同步回查后由有权限人员确认。只读同步任务最多重试5次，可在页面重试失败任务。

商户实际 `NO_AUTH` 等错误会显示在页面/操作记录中；保存凭据不表示微信已授予权限。渠道调用使用渠道号及配套凭据签名，`complainted_mchid` 使用商户的微信子商户号；所属支付机构仅用于业务归属。

## 本机已配置环境

当前目录源码基于服务器 `8.136.45.231` 的发布版本 `820d8c7`，在此基础上新增渠道代调用和凭据上传。本地使用独立 Python 3.12 虚拟环境及 MySQL 8.4 实例，沿用原有开发服务脚本和演示模式。

- 页面：`http://127.0.0.1:8316/`；账号与权限后台：`http://127.0.0.1:8316/admin/`。
- 账号：`admin`；密码查看本机 `~/.local/share/kesu-local/login.json` 的 `admin_password` 字段。
- 双击仓库根目录的 `启动本地.command` 启动 MySQL、Web 和 worker；双击 `停止本地.command` 停止这三个本项目进程并保留数据。
- 命令行在仓库根目录执行 `sh scripts/local.sh start`、`sh scripts/local.sh status` 或 `sh scripts/local.sh stop`。修改 Python 代码后停止再启动；现有启动脚本不启用自动重载。
- 本地数据库监听 `127.0.0.1:13316`，库名 `kesu_local`；配置、数据库文件及日志位于 `~/.local/share/kesu-local/`，不随源码提交。运行目录权限0700，配置及登录凭据权限0600。
- 本地已启用 `DEBUG=true` 与 `ALLOW_DEMO=true`，包含3个演示商户、12条演示投诉。演示商户使用项目自带模拟接口；未复制线上数据库或商户密钥，不代表真实微信链路已接通。

管理命令使用同一配置，例如：

```bash
COMPLAINT_CONFIG="$HOME/.local/share/kesu-local/config.json" .venv/bin/python manage.py check
```

此配置依赖当前电脑上的 `/opt/homebrew/opt/mysql@8.4`。迁移到其他电脑时，按下面的新环境步骤初始化，不复制或覆盖已有数据库的 `DATA_KEY`。页面回归会改变本地演示投诉状态；再次运行 `seed_demo` 不会重置已有投诉。

## 新环境运行

安装 Python 3.12、MySQL 8.0+、系统 MySQL 客户端开发库，创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Linux 可先安装 `default-libmysqlclient-dev pkg-config`；macOS 使用对应 MySQL 开发库。库无法自动发现时，使用 mysqlclient 原生 `MYSQLCLIENT_CFLAGS` / `MYSQLCLIENT_LDFLAGS` 指定实际目录。

创建独立数据库和用户；密码由部署人员设置：

```sql
CREATE DATABASE complaint_desk CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'complaint_app'@'localhost' IDENTIFIED BY '<实际强密码>';
GRANT ALL PRIVILEGES ON complaint_desk.* TO 'complaint_app'@'localhost';
```

复制 `config.example.json` 到仓库外受限目录，填写数据库、域名及两个独立随机密钥，文件权限0600。生产 `DEBUG=false`；演示另加 `DEBUG=true` 和 `ALLOW_DEMO=true`。环境变量可覆盖JSON。

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
export COMPLAINT_CONFIG=/安全目录/complaint-config.json
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8316
```

**DATA_KEY 必须备份，已有数据后不能直接更换。** 丢失会导致商户密钥及敏感内容不可读取。商户私钥权限0600或0640，路径须为绝对路径，不可位于公开静态目录。

单独运行同步工作进程：

```bash
COMPLAINT_CONFIG=/安全目录/complaint-config.json .venv/bin/python manage.py complaint_worker
```

`--once` 仅处理当前到期队列，不创建定时补拉。超过15分钟的运行任务可被恢复。重投只涉及查询和本地同步，微信写接口不会自动重发。

生产用进程管理器分别管理 gunicorn 和 worker：

```bash
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8316 --workers 2
.venv/bin/python manage.py complaint_worker
```

由反向代理提供公网 HTTPS 和 `/static/` 静态资源。`TRUST_PROXY=true` 只用于受信任代理独占入口且会正确清除/设置 `X-Forwarded-Proto` 的环境。`GET /health/` 检查应用与数据库，不证明商户权限、worker存活或真实微信链路已通过。

容器部署使用仓库Dockerfile，Python 3.12运行镜像与依赖构建阶段分开；源码及静态资源在镜像内，运行时通过只读挂载的仓库外配置传入 `COMPLAINT_CONFIG`。网页和worker使用同一镜像，worker命令为 `python manage.py complaint_worker`；MySQL 8.0+独立保留数据卷。更新镜像前执行迁移，反向代理通过HTTPS提供页面及收集后的静态目录。服务配置、数据库卷、DATA_KEY及登录凭据须保留，镜像不含它们。

## 商户接入与权限

选择“商户自行调用”时，在商户管理填写商户号、对应商户API证书序列号、私钥路径、32字节APIv3密钥、微信验签ID和公钥/平台证书路径。编辑密钥留空保留原值。每个自行调用商户的公网通知路径为：

```text
https://你的域名/callbacks/实际商户号/
```

在“通知配置”先查询已有地址并确认用途，再创建/更新及回查。删除停止主动推送；仅保存商户资料不会注册。通知入口无需后台登录，以微信签名校验来源。

选择“渠道代调用”前，在“商户号管理 → 渠道账号”新增微信渠道账号并维护上述凭据。新增商户时选择该渠道、填写微信子商户号，商户不重复配置证书或密钥。支付机构商户编号可用于列表查询；多个渠道账号可分别管理。

渠道账号分别上传 `apiclient_cert.pem`（API证书）、`apiclient_key.pem`（配套私钥）和微信公钥／平台证书 PEM，单个文件最多64KB。保存时自动读取证书序列号、核对证书所属渠道号及证书与私钥是否配套。编辑时不上传新文件则保留原值；单独更新证书时可复用匹配的现有私钥。微信公钥 ID 需要自行填写，不能填入商户 API 证书序列号字段。备用验签材料为可展开的可选项。APIv3 密钥在列表和编辑页显示“已保存／未配置”，不回显明文；编辑时留空保留原值。

上传文件默认保存在配置 JSON 同级的 `channel-credentials/`，可通过 `CHANNEL_CREDENTIAL_ROOT` 指定独立目录。目录必须位于源码和静态目录之外，并由应用运行用户写入；文件权限0600，材料目录0700。替换文件生成独立新目录，保留旧文件；表单或数据库保存失败时不替换原配置。保存资料和上传文件都不会自动请求微信。

容器部署文件上传能力时，需为 `CHANNEL_CREDENTIAL_ROOT` 增加可写、持久化目录，例如 `./shared/channel-credentials:/run/complaint/channel-credentials:rw`，并让应用用户（当前镜像 UID 10001）拥有该目录。现有 `shared/keys` 的只读挂载继续用于原商户凭据。

渠道投诉通知统一在渠道账号的“通知配置”管理，路径为 `https://你的域名/callbacks/channels/微信渠道号/`。写入前须先查询现有微信配置，提交时会再次核对地址是否变化，避免覆盖未经核对的现有地址。渠道回调验签、解密后，通知与任务在同一数据库事务中保存并返回204；worker查询投诉详情确认微信子商户归属，复用原同步流程。未在该渠道下接入的子商户不会自动创建或导入；失败任务最多重试5次，可在渠道通知页面重试。

已有业务记录的商户不能通过表单改换接入方式或渠道，以防历史任务串用账号。渠道账号有关联记录时受删除保护，可以停用。保存或编辑渠道及商户资料不会自动请求微信。

列表来自本地数据。首次点击“同步微信投诉”，等待worker并刷新；新追问看协商历史。退款用户、高风险及即时服务单应优先处理。平台客服协助期间商户留言可能不向用户展示。

结果未知时先同步详情、核对协商历史/状态，再在“操作记录”确认已执行或未执行。通知配置先查询微信地址。提交中至少等待5分钟才允许人工确认，避免覆盖仍在执行的请求。

沿用 Django User、Group、Permission。运营可授予 `view_complaint`、`view_merchant`、`sync_complaint`、`reply_complaint`、`view_operation`；退款、完结、通知地址、人工结果确认分别为 `refund_complaint`、`complete_complaint`、`configure_notification`、`change_operation`。商户CRUD使用原生对应权限，账号在Django后台创建并授权。

渠道账号使用独立的 `view_wechatchannel`、`add_wechatchannel`、`change_wechatchannel`、`delete_wechatchannel` 权限；渠道通知管理还需要 `configure_notification`，失败任务重试需要 `sync_complaint`。已有账号不会自动获得新增管理权限。

当前权限按系统操作划分，有投诉查看权限的人员可查看全部商户；本需求未要求按账号限制可见商户。

## 接口覆盖

微信调用固定到服务端 `https://api.mch.weixin.qq.com`，浏览器不接触商户私钥。

| 能力 | 微信路径（前缀 `/v3/merchant-service`） | 本系统入口 |
|---|---|---|
| 列表 | GET `/complaints-v2` | 批量同步任务 |
| 详情 | GET `/complaints-v2/{id}` | 同步详情任务 |
| 协商历史 | GET `/complaints-v2/{id}/negotiation-historys` | 同步详情任务 |
| 回复 | POST `/complaints-v2/{id}/response` | `POST /merchants/{m}/complaints/{c}/reply/` |
| 完结 | POST `/complaints-v2/{id}/complete` | `POST .../complete/` |
| 退款审批 | POST `/complaints-v2/{id}/update-refund-progress` | `POST .../refund/` |
| 即时服务 | POST `/complaints-v2/{id}/response-immediate-service` | `POST .../immediate/` |
| 图片上传 | POST `/images/upload` | `POST .../upload/` |
| 图片下载 | GET `/images/{media_id}` | `GET .../image/?id=...` |
| 通知配置 | GET/POST/PUT/DELETE `/complaint-notifications` | `/merchants/{m}/notifications/` 表单对应方法 |

内部 `{m}`、`{c}` 是本地ID；微信投诉号和商户号由后端从归属记录取得。内部写操作要求登录、对应权限及CSRF。上传字段为 `file`。未开放通用微信请求代理。

官方依据：[消费者投诉开发指引](https://pay.weixin.qq.com/doc/v3/merchant/4012068499)、[回复用户](https://pay.weixin.qq.com/doc/v3/merchant/4012467254)、[退款审批](https://pay.weixin.qq.com/doc/v3/merchant/4012467256)、[即时服务消息](https://pay.weixin.qq.com/doc/v3/merchant/4017151596)。2026-10-05用独立Playwright无头Chromium核对接口文档。

## 验证与范围

```bash
COMPLAINT_CONFIG=/安全目录/测试配置.json .venv/bin/python manage.py test desk --noinput
```

测试使用独立MySQL测试库，用户须有 `test_数据库名` 的建库/迁移权限。回归用真实RSA/AES-GCM和签名模拟响应，覆盖签名、应答篡改/过期/错误ID、手机号解密、回调去重及保存失败、加密落库、幂等/未知结果、日期/分页、队列重试、权限/CSRF和商户隔离。`desk/test_channels.py` 额外验证渠道签名与子商户参数分离、四种处理操作及图片复用、无子商户号通知的异步归属、原子入队、渠道权限和通知配置查询保护；不调用真实微信。

页面回归仅用于可丢弃的独立开发数据库初始演示数据，会改变演示投诉状态：

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium
.venv/bin/python manage.py seed_demo
.venv/bin/python scripts/check_ui.py --url http://127.0.0.1:8316 --credentials /安全目录/演示登录.json --output /临时目录/页面证据
```

演示登录JSON含 `admin_password`，账号admin。只有 `DEBUG=true` 且 `ALLOW_DEMO=true` 可建立/使用演示商户；演示图片下载为占位图，真实模式从微信读取原图。Playwright独立启动无头Chromium，完成/失败均关闭，不复用个人浏览器。

渠道配置页面可单独验收，仅支持本地地址；脚本创建不含微信凭据的临时渠道和商户，完成后通过页面删除：

```bash
.venv/bin/python scripts/check_channels_ui.py --url http://127.0.0.1:8316 --credentials /安全目录/演示登录.json --output /临时目录/渠道页面证据
```

上传及密钥保存状态验收只使用临时生成的证书和停用渠道，完成后清理测试账号与文件：

```bash
.venv/bin/python scripts/check_channel_upload_ui.py --url http://127.0.0.1:8316 --config /安全目录/本地配置.json --credentials /安全目录/演示登录.json --output /临时目录/上传页面证据
```

详情布局修改还需在桌面、平板及390px视口展开订单、协商补充信息和最近操作，检查长商户名、64位投诉号、128位订单号、连续字符/JSON和长错误提示是否完整换行；弹窗左侧资料与右侧会话分别滚动，右侧回复框不随记录滚动。手机先展开“投诉信息”再使用审批等处理按钮，收起后恢复聊天空间。原生后台同步检查投诉、任务、操作详情的标题、面包屑和只读字段。页面回归脚本包含退款审批及通知配置的手机确认项对齐检查和截图。

真实商户凭据、微信权限、公网HTTPS回调、真实微信图片/回复展示及退款渠道需提供并在授权测试投诉上联调。本次不声称真实商户已接通，不含部署。
