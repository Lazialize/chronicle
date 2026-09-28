# Chronicle

Graphiti の公式 MCP サーバーを、固定 Bearer 認証と HTTPS 付きで EC2 に配置する構成。
東京リージョン (`ap-northeast-1`)、Cloudflare 管理の `specite.dev` を想定。
サブドメインの初期値は `chronicle.specite.dev`（変更可能）。

```text
MCP client (Authorization: Bearer …)
  → https://chronicle.specite.dev/mcp/
  → Cloudflare DNS (A record, DNS only)
  → Elastic IP → EC2:443 → Caddy → Graphiti MCP → FalkorDB
                                      │
                                      └→ OpenAI API (LLM / embeddings)
```

Compose は公式イメージを digest で固定している。外部に公開するのは Caddy の
80/443 のみ。Graphiti:8000 と FalkorDB:6379 は Docker 内で接続し、DB のブラウザ UI
は無効にする。DB は内部ネットワークで隔離し、Caddy からも直接到達できない。
Caddy が証明書の取得・更新を行うので ALB は不要。

## AWS の準備

`infra/ec2.yaml` は EC2、EIP、80/443 のみ許可する Security Group、SSM 用 IAM ロールを
作成する。既存 VPC と、その VPC に属する Internet Gateway への経路がある public subnet
を指定する。VPC の DNS を有効にしておく。SSH の開放・鍵は不要。

初期サイズは `t3.medium` / gp3 40 GiB。実データ量とメモリ使用量を見て調整する。
EC2・EBS・public IPv4・通信・LLM/embedding API に料金が発生する。
以下の deploy は実際にリソースを作成するため、AWS profile とパラメータを確認して実行する。

```sh
aws cloudformation deploy \
  --profile YOUR_PROFILE --region ap-northeast-1 \
  --stack-name chronicle --template-file infra/ec2.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides VpcId=vpc-REPLACE PublicSubnetId=subnet-REPLACE

aws cloudformation describe-stacks \
  --profile YOUR_PROFILE --region ap-northeast-1 \
  --stack-name chronicle --query 'Stacks[0].Outputs'
```

Cloudflare に次の DNS レコードを追加する（既存レコードがあれば競合を確認）：

| Type | Name | Content | Proxy status |
| --- | --- | --- | --- |
| A | chronicle | スタック出力の PublicIp | DNS only |

同名の古い AAAA レコードは残さない。この構成の TLS はブラウザ/クライアントと Caddy の間で
終端する。Cloudflare のプロキシや Access は使わない。

## EC2 で起動

AWS コンソールの Session Manager または `aws ssm start-session` で接続する。
cloud-init の完了を待ち、`docker compose version` を確認する。
失敗時は `/var/log/cloud-init-output.log` を確認。

リポジトリを EC2 に転送し、`compose.yaml`、`Caddyfile`、`config/`、`.env.example`
を `/opt/chronicle` に配置する（公開済みリポジトリなら git clone、非公開なら認証した
git clone や管理用の転送経路を利用）。コードの配置・秘密情報の投入は UserData に含めない。

```sh
sudo -i
cd /opt/chronicle
umask 077
cp .env.example .env
openssl rand -hex 32
# エディタで .env を設定:
# MCP_DOMAIN, ACME_EMAIL, MCP_BEARER_TOKEN（上で生成した値）, OPENAI_API_KEY
# MODEL_NAME は API アカウントで利用可能なモデルに設定
chmod 600 .env
docker compose config --quiet
docker compose up -d --wait --wait-timeout 300
docker compose ps
```

API キーは LLM による情報抽出と embedding に使用する。最初の構成は OpenAI を使用。
モデル/API プロバイダーは `config/graphiti.yaml` で変更できる。
embedding モデルを変更する場合は次元数と既存データとの互換性も確認する。
秘密情報を含むため `docker compose config` の通常出力や `.env` を共有しない。

## 接続と検証

Streamable HTTP と任意の Authorization ヘッダーに対応した MCP クライアントを使用する。
設定項目名はクライアントによって異なるが、接続情報は以下。

```text
URL: https://chronicle.specite.dev/mcp/
Authorization: Bearer <MCP_BEARER_TOKEN>
```

これは共有トークン方式で、OAuth discovery/login は提供しない。
OAuth 必須のクライアントには別途対応が必要。
トークンを持つ利用者には Graphiti の読み書き・削除ツールを含む全権限がある。
`group_id` は整理用で、利用者間のアクセス制御にはならない。

未認証は `401` になる：

```sh
curl -i https://chronicle.specite.dev/mcp/
```

認証付きで MCP の initialize と tools/list を実行し、ツール一覧を確認する。
Graphiti は正常な initialize でも JSON または SSE で応答するため、HTTP GET だけで
接続成功とは判断しない。HTTPS の証明書エラーを `-k` で回避しない。

Origin が付くリクエストは `https://chronicle.specite.dev` のみ許可する。
Origin のないネイティブクライアントは使用可能。Web クライアントを使う場合は、その
Origin を明示的に許可する設定と CORS の検討が必要。

固定した公式イメージは `/mcp` を提供し、MCP SDK はデフォルトで localhost の Host のみ
受け付ける。Caddy は公開 Host と Origin を検証した後、`/mcp/` を内部で `/mcp` に補正し、
内部接続の Host を `localhost:8000` に設定する。検証済み Origin と Bearer は upstream に
転送しない。このため Graphiti のポートを直接公開してはいけない。

## 運用

- Caddy の証明書と FalkorDB データは named volume に保存する。
  FalkorDB は AOF / everysec を有効にするが、これはバックアップではない。
- EBS スナップショット等で別途定期バックアップを設定する。整合性を優先した初回バックアップは、
  Graphiti の処理が完了してから `docker compose stop graphiti falkordb`、EBS snapshot の
  作成、`docker compose up -d` の順で行う。復元も検証する。
- 公式 Graphiti の追加処理は非同期キューを使う。受付成功は DB 永続化の完了ではない。
  再起動前にキューが処理済みか確認する。
- トークン変更は `.env` を更新し `docker compose up -d --force-recreate caddy`。
  既存の長時間接続も切断する。クライアント側の値も更新する。
- バージョン更新はイメージ digest を意図的に更新し、バックアップ後に検証する。
  `docker compose down -v` はデータと証明書を削除するので実行しない。
- 単一 EC2 のためインスタンス障害・保守時には停止する。
- テンプレートは誤削除を避けるため EC2 の削除/置換時に旧インスタンスを保持し、
  root EBS も終了時に保持する。スタック削除だけでは全リソースの課金は止まらない。
  スタック削除は保持 EC2 の依存関係で失敗する場合もあるため、廃止はデータ退避後に
  EC2・EBS・EIP・IAM・Security Group の残存を確認して行う。

## ローカル検証

```sh
# Caddy バイナリが PATH 上にある場合（テスト用 upstream のみ、API 課金なし）
python3 -m unittest discover -s tests -v

# Docker 起動後、実コンテナで HTTPS・MCP・DB 永続化を検証（API 課金なし）
python3 tests/docker_smoke.py
```

このテストは認証境界、Origin、MCP 用 HTTP メソッドの転送を確認する。
Docker smoke test は独立した Compose project を作り、実際の Graphiti + FalkorDB を起動する。
テスト専用 CA を使って証明書とホスト名を検証し、MCP 初期化・ツール一覧・get_status、
FalkorDB のテストグラフの再起動後の保持を確認する。終了時はテスト環境だけを削除する。
実 API キーを使用せず Graphiti の外向きネットワークも遮断するため、LLM API の呼び出しはない。
LLM を使う記憶の抽出・検索、公開証明書の発行はデプロイ環境で別途確認が必要。

## 参照

- [公式 Graphiti MCP](https://github.com/getzep/graphiti/tree/main/mcp_server)
- [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [MCP HTTP transport / Origin 検証](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
