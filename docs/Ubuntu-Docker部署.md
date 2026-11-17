# Ubuntu Docker 部署

## 1. 前置条件

服务器需要安装 Docker Engine 和 Docker Compose 插件。未安装时可使用 Docker 官方软件源：

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker run --rm hello-world
```

确认以下命令可用：

```bash
docker --version
docker compose version
```

将整个项目目录上传到服务器，例如 `/opt/stock-analysis`，然后进入该目录：

```bash
cd /opt/stock-analysis
```

## 2. 构建并启动

```bash
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f stock-analysis
```

容器启动后会直接运行看板和 API，不需要激活、许可证或机器码。运行日志由 Docker 管理，
通过 `docker compose logs` 查看。

## 3. 访问看板

项目没有用户登录或 API 鉴权，因此默认只绑定 Ubuntu 的 `127.0.0.1`。推荐在自己的电脑
建立 SSH 隧道：

```bash
ssh -L 8795:127.0.0.1:8795 <Ubuntu用户名>@<服务器IP>
```

保持 SSH 会话打开，在本机浏览器访问：

```text
http://127.0.0.1:8795/
```

此时会直接进入看板。健康检查地址为：

```text
http://127.0.0.1:8795/api/health
```

如果只在可信局域网内使用，需要通过服务器 IP 直接访问，可在项目目录创建 `.env`：

```bash
cp .env.example .env
sed -i 's/STOCK_BIND_ADDRESS=127.0.0.1/STOCK_BIND_ADDRESS=0.0.0.0/' .env
sudo docker compose up -d
```

然后访问 `http://<服务器IP>:8795/`。同时应使用 Ubuntu 防火墙把 `8795` 端口限制为
可信来源 IP，不要直接向整个公网开放。

## 4. 日常操作

查看状态和日志：

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 -f stock-analysis
```

更新代码后重新构建：

```bash
sudo docker compose up -d --build
```

停止或再次启动：

```bash
sudo docker compose stop
sudo docker compose start
```

删除容器和项目网络：

```bash
sudo docker compose down
```

## 5. 公网访问说明

项目没有用户登录认证，因此默认使用 `127.0.0.1` 绑定，并推荐通过 SSH 隧道访问。
不要仅把 `.env` 中的 `STOCK_BIND_ADDRESS` 改为 `0.0.0.0` 后直接开放公网端口。
确需公网或多人访问时，应在前面配置带 HTTPS 和身份认证的反向代理，并继续让本服务只监听宿主机回环地址。

## 6. 海外服务器访问行情源

默认恢复原版可用的直连及东方财富兼容解析：`push2.eastmoney.com`、
`push2his.eastmoney.com` 及后者的子域名连接到 `117.184.45.167`，
HTTPS仍使用原域名进行Host/SNI和证书校验。腾讯等其他域名使用系统DNS。
2026-11-16同机对照中，原版可获取实时资金流，取消兼容解析后失败，恢复后重新成功。
这是恢复已验证的旧版请求路径，并不表示该固定地址在所有服务器上都可用。

项目 `.env` 可配置以下选项，默认无需修改：

```dotenv
STOCK_EASTMONEY_DNS_MODE=legacy
STOCK_DATA_PROXY=
```

`STOCK_EASTMONEY_DNS_MODE=system` 可关闭兼容解析，改用系统DNS。
行情请求默认直连，不自动读取系统或宿主机的 `HTTP_PROXY`、`HTTPS_PROXY`。
确需代理时，将 `STOCK_DATA_PROXY` 设置为容器内可访问的HTTP或HTTPS代理地址。
宿主机或个人电脑的 `127.0.0.1` 不是容器内可直接使用的代理地址。
通过代理建立HTTPS隧道时，由代理端解析目标域名。

分时资金流优先使用腾讯 `proxy.finance.qq.com` 的 `hsfundtab` 接口，
主源最多等待3秒；失败或无记录才回退东方财富，东财内部多个节点并行查询。
接口总等待预算仍为8秒，浏览器请求上限10秒。资金流加载独立于报价和K线。
日级历史资金流和市场宽度仍使用东方财富；页面会标出分时数据的实际来源和统计口径。

修改代码或这些环境变量后，使用 `docker compose up -d --build` 重新创建服务。
仅重启旧容器不会更新其中的代码。若资金流仍失败，查看
`docker compose logs --tail=100 stock-analysis` 中资金流接口的上游连接错误，
再区分DNS失败、连接超时、HTTP拒绝和无记录，不能仅凭服务器所在地区判断原因。

本次腾讯分时资金流接入后，访问 `/api/health` 应包含 `"flow_api_version": 4`，
以及 `"flow_sources": ["腾讯", "东方财富"]`。
若版本低于4或无此字段，说明运行服务尚未加载本次修复。代码更新后须重新构建镜像；
仅刷新浏览器或重启旧容器不会加入新代码。此字段只能证明版本已更新，资金流是否可用
仍以 `/api/realtime_flow?symbol=513300` 的 `status`、`as_of` 和错误信息为准。
