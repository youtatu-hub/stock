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
