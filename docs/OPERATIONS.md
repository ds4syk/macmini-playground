# 运维说明

## 组件

| 位置 | 组件 | 作用 |
| --- | --- | --- |
| Mac mini | `paper_trader` + SQLite | 行情、回放、撮合、估值、模型调用、导出 |
| Mac mini | launchd `com.suyingke.papertrader.daily` | 工作日 15:35 触发 `scripts/daily.sh` |
| 腾讯云 | Caddy | TLS、token 路径校验、静态文件 |
| 腾讯云 | `/srv/paper/<token>/` | 仪表盘静态文件（只读挂载进 caddy） |
| Cloudflare | `paper.suyingke.chat A 43.134.76.212` | DNS only，DNS-01 签发证书 |

## 日常

```bash
# 手动结算今天 + 生成明天决策 + 重新部署
python3 -m paper_trader.runner daily
scripts/deploy.sh --skip-export

# 只更新前端
scripts/deploy.sh --skip-export

# 查看状态
python3 -m paper_trader.runner status
```

`daily` 的判定：以本地权益表最后日期为起点，把之后每个交易日依次结算（执行上一交易日收盘后生成的决策 → 收盘估值 → 生成下一交易日决策）。机器休眠导致漏跑时会自动补跑，但每个漏掉的交易日都会真实调用模型。

## token 轮换

1. 编辑 `~/.config/paper-trader/deploy.env` 的 `PAPER_TOKEN`（建议 `openssl rand -hex 16`）。
2. `scripts/deploy.sh --skip-export`。
3. 旧路径立即 404；Caddyfile 旧版本备份为 `Caddyfile.bak-paper-*`。

## 腾讯云前置条件（一次性）

1. Cloudflare DNS 记录：`paper.suyingke.chat` → `43.134.76.212`，**DNS only**（灰云），避免 CDN 缓存 token 路径。
2. Caddy 容器增加只读挂载：

   ```yaml
   # ~/new-api/docker-compose.yml, caddy.volumes
   - /srv/paper:/srv/paper:ro
   ```

   然后 `docker compose up -d --no-build caddy`。Caddy 容器需要能看到宿主机 `/srv/paper`；仅写 Caddyfile 不够。
3. 站点块由 `scripts/update_caddy.py` 幂等写入，包含：

   ```caddy
   paper.suyingke.chat {
       tls { dns cloudflare {env.CF_API_TOKEN} }
       handle /<token> { redir /<token>/ 308 }
       handle_path /<token>/* {
           root * /srv/paper/<token>
           encode gzip
           file_server
           header { Cache-Control "no-store" ... }
       }
       handle { respond 404 }
   }
   ```

4. `caddy validate` 通过后 `caddy reload`。

## 故障排查

| 症状 | 检查 |
| --- | --- |
| 链接 404 | `ls /srv/paper/<token>/`；Caddyfile 中 token 是否与 deploy.env 一致；`/srv/paper` 是否挂载进容器 |
| 证书错误 | `docker logs caddy | grep paper.suyingke`；Cloudflare token 是否有 DNS 编辑权限 |
| 数据不更新 | `python3 -m paper_trader.runner status`；`/tmp/paper-trader-daily.log`；launchd `launchctl print gui/$(id -u)/com.suyingke.papertrader.daily` |
| 模型调用失败 | `decisions.error` 非空时组合保持持仓；查看 runner 输出与 provider 状态 |
| 行情失败 | 腾讯接口有频控/反爬；本地缓存仍可用于回放，稍后重试 `fetch` |

## 数据与密钥

- SQLite：`data/trader.db`（不入 git）
- 模型密钥：`~/.config/paper-trader/.env`（600）
- 部署 token：`~/.config/paper-trader/deploy.env`（600）
- 前端：`web/` + `web/data.json`（data.json 不入 git）
- 截图与结果快照：`docs/screenshots/`、`docs/RESULTS.md`
